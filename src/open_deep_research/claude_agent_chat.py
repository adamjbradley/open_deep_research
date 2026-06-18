"""CLI-driven (subscription/login) LLM + search backends for all LLM activity.

This module lets Open Deep Research run every LLM call through a local agent CLI
so usage bills against a subscription/login rather than per-token API credits:

- ``ClaudeAgentChat`` -- Claude Code via the ``claude_agent_sdk`` (Claude Pro/Max).
- ``GeminiCLIChat`` -- Google's ``gemini`` CLI (Google login / free tier).
- ``CodexCLIChat`` -- OpenAI's ``codex`` CLI (ChatGPT login).

Backend is selected per role by a provider prefix on the model string
("claude:opus", "gemini:2.5-pro", "codex:gpt-5") via ``build_chat_model`` /
``parse_backend``. Claude has a first-class SDK with native structured output;
the Gemini/Codex CLIs lack that, so structured output and tool-call emission are
coerced by asking the CLI for the JSON tool-selection envelope and parsing it
back -- the same ``AIMessage.tool_calls`` contract across all three.

Core exports:

- ``ClaudeAgentChat`` -- a ``BaseChatModel`` that wraps ``claude_agent_sdk.query``.
  It supports plain-text generation, structured output (via ``with_structured_output``),
  and tool-call emission (via ``bind_tools``). The graph keeps ownership of the
  agentic loop: we always run the SDK with ``allowed_tools=[]`` so it never
  executes tools itself; instead it *selects* a tool and we hand the resulting
  ``AIMessage.tool_calls`` back to LangGraph for execution.

- ``configurable_claude_model`` -- a drop-in replacement for the
  ``init_chat_model(configurable_fields=...)`` model used in deep_researcher.py.
  It mimics the small slice of ``_ConfigurableModel`` behaviour the codebase
  relies on: the flat ``with_config({"model": ..., "max_tokens": ..., "api_key": ...})``
  pattern, plus declarative ``with_structured_output`` / ``bind_tools`` /
  ``with_retry`` chaining that is replayed against the real model at invoke time.

Design notes
------------
* ``with_structured_output`` (LangChain base impl, ``method="function_calling"``)
  calls ``bind_tools([schema])`` then parses the emitted tool call. Because our
  ``bind_tools`` produces a real ``AIMessage`` with ``tool_calls``, structured
  output "just works" on top of the same mechanism as supervisor/researcher tools.
* Tool selection is forced via the SDK ``output_format`` (a JSON-schema "envelope"
  listing the available tool names); the tool catalog + schemas are also injected
  into the system prompt so arguments conform.
* Per-call ``max_tokens`` has no SDK equivalent and is accepted-but-ignored.
* Model selection is mapped to a family / model id the SDK understands
  (``opus`` / ``sonnet`` / ``haiku`` / full ``claude-*`` id).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any, Callable, Optional, Sequence
from weakref import WeakKeyDictionary

import claude_agent_sdk as cas
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool

logger = logging.getLogger(__name__)

# Limit concurrent SDK subprocesses. The subscription tier throttles around a
# handful of simultaneous sessions, and the app fans out parallel researchers.
_MAX_CONCURRENCY = int(os.getenv("CLAUDE_SDK_MAX_CONCURRENCY", "4"))
# One semaphore PER event loop. A single module-global Semaphore binds to whichever
# loop first awaits it and is never rebound; under langgraph dev (worker-thread
# Proactor loops via _offload_subprocess, or per-request loops) a later acquire from a
# different loop raises "RuntimeError: <Semaphore> is bound to a different event loop"
# -- a hard, non-retryable model-call failure. Keying by the running loop avoids that;
# WeakKeyDictionary lets entries drop when a loop is garbage-collected.
_SEMAPHORES: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
    """Return the concurrency semaphore bound to the currently-running event loop."""
    loop = asyncio.get_running_loop()
    sem = _SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _SEMAPHORES[loop] = sem
    return sem


# How long to wait for a single SDK query (model turn or web-search session) before
# treating the subprocess as hung. Without this a stuck ``claude.exe`` blocks the
# consume forever, which freezes the graph's un-timed ``asyncio.gather`` and leaves
# the whole run wedged. Shares the CLI backend's budget so all backends behave alike.
_SDK_TIMEOUT_S = int(os.getenv("CLI_BACKEND_TIMEOUT", "600"))
# Bounded retry for transient/spurious SDK & CLI failures (see _is_transient_sdk_error).
_SDK_MAX_ATTEMPTS = max(1, int(os.getenv("CLAUDE_SDK_MAX_ATTEMPTS", "3")))
_SDK_RETRY_BACKOFF_S = float(os.getenv("CLAUDE_SDK_RETRY_BACKOFF", "1.5"))
# After the drain timeout fires, how long to let cancellation try to tear the subprocess
# down before ABANDONING a wedged task rather than freezing the run. A CLI subprocess can
# wedge in uninterruptible I/O (kernel 'D' state) and not die even on SIGKILL until the
# call returns; we won't block the whole run waiting for that. See _drain_query_with_timeout.
_DRAIN_REAP_GRACE_S = float(os.getenv("CLI_DRAIN_REAP_GRACE", "10"))

# Substrings that mark a failure as transient -- worth retrying rather than surfacing.
# "error result: success" is the contradictory envelope the CLI emits under load
# (is_error=true with subtype "success" and no error detail, then a non-zero exit);
# it carries no actionable information, so a retry is the right response.
_TRANSIENT_ERROR_MARKERS = (
    "error result: success",
    "error result: unknown error",
    "overloaded",
    "rate limit",
    "rate_limit",
    " 429",
    "failed (exit ",  # matches _run_cli's "CLI <bin> failed (exit <code>)" message
    "process error",
    "processerror",
    "connection reset",
    "connection error",
    "broken pipe",
    "timed out",
    "timeout",
)


def _is_transient_sdk_error(exc: BaseException) -> bool:
    """Whether a failure from the SDK/CLI path is transient and worth retrying.

    Timeouts are always transient. Otherwise we match known overload/connection/
    contradictory-envelope signatures in the message. Genuine errors (schema
    mismatches, missing keys, config problems) are NOT transient -- they must
    surface immediately so real bugs aren't masked by retries.
    """
    # Keep both: on Python 3.10 asyncio.TimeoutError is a distinct class (merged with
    # the builtin in 3.11). PEP-604 union keeps the linter happy on 3.10+.
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


async def _run_with_retry(attempt, *, max_attempts: int, backoff_s: float):
    """Run ``attempt()`` with bounded retries on transient failures only.

    Retries with exponential backoff while ``_is_transient_sdk_error`` holds and
    attempts remain; re-raises non-transient errors immediately and the last
    transient error once attempts are exhausted.
    """
    max_attempts = max(1, max_attempts)  # always attempt once; never fall through to None
    for i in range(max_attempts):
        try:
            return await attempt()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless transient
            if not _is_transient_sdk_error(exc) or i >= max_attempts - 1:
                raise
            if backoff_s:
                await asyncio.sleep(backoff_s * (2 ** i))


def _detach(task: asyncio.Task) -> None:
    """Let an abandoned task's eventual exception be retrieved silently (no warning)."""
    def _swallow(t: asyncio.Task) -> None:
        try:
            if not t.cancelled():
                t.exception()
        except Exception:  # noqa: BLE001 - the task was abandoned; its outcome is irrelevant
            pass
    task.add_done_callback(_swallow)


async def _drain_query_with_timeout(prompt, options, handler, timeout_s: float) -> None:
    """Consume ``cas.query`` feeding each message to ``handler``, bounded by a timeout.

    Hardening against a wedged subprocess: a plain ``asyncio.wait_for(_drain(), t)`` does
    fire its timeout, but then *awaits the inner task's cancellation* -- and that
    cancellation blocks while it tries to tear down a CLI subprocess stuck in uninterruptible
    I/O (kernel 'D' state), which never returns. The whole run then freezes despite the
    timeout. Instead we time the drain with a SHIELD (so the timeout doesn't await
    cancellation), then cancel and give a bounded grace; if the subprocess still won't die
    we ABANDON the task (it self-completes when the kernel I/O eventually returns) and raise
    TimeoutError so the caller (`_run_with_retry` -> failover) makes progress instead of hanging.
    """
    async def _drain() -> None:
        async for msg in cas.query(prompt=prompt, options=options):
            handler(msg)

    task = asyncio.ensure_future(_drain())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        return
    except asyncio.TimeoutError:
        pass  # timed out -> bounded teardown below (a real error would have propagated)

    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_DRAIN_REAP_GRACE_S)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 - any teardown error is irrelevant; we're abandoning
        pass
    if not task.done():
        _detach(task)  # wedged in I/O: abandon rather than freeze the run
    raise asyncio.TimeoutError(
        f"CLI subprocess drain exceeded {timeout_s}s (subprocess may be wedged in I/O)")


def use_subscription() -> bool:
    """Whether to bill against the Claude subscription (vs. ANTHROPIC_API_KEY)."""
    return os.getenv("CLAUDE_USE_SUBSCRIPTION", "true").lower() == "true"


_CLI_CWD: Optional[str] = None


def _cli_cwd() -> str:
    """A neutral empty working directory for CLI backends.

    Agent CLIs (gemini, codex) ingest the working directory as project context
    and switch to coding-agent mode. Running them in an empty dir keeps them
    behaving as plain LLMs that answer the prompt.
    """
    global _CLI_CWD
    if _CLI_CWD is None or not os.path.isdir(_CLI_CWD):
        import tempfile

        _CLI_CWD = tempfile.mkdtemp(prefix="odr_cli_cwd_")
    return _CLI_CWD


def to_claude_model(model_name: Optional[str]) -> str:
    """Map a configured model string to something the Agent SDK understands.

    Accepts SDK families ("opus"/"sonnet"/"haiku"), full ids ("claude-opus-4-8"),
    "anthropic:claude-..." prefixes, and tolerates legacy "openai:gpt-4.1"-style
    values by falling back to a sensible Claude family.
    """
    if not model_name:
        return "sonnet"
    s = model_name.strip()
    low = s.lower()
    # Explicit Claude id / family, optionally prefixed with a provider.
    if low.startswith("anthropic:"):
        s = s.split(":", 1)[1]
        low = s.lower()
    if low.startswith("claude") or low in {"opus", "sonnet", "haiku"}:
        return s
    # Keyword fallback for any other provider string.
    if "haiku" in low:
        return "haiku"
    if "opus" in low:
        return "opus"
    if "sonnet" in low:
        return "sonnet"
    return "sonnet"


def to_gemini_model(model_name: Optional[str]) -> str:
    """Map a configured model string to a Gemini CLI model id.

    Accepts "gemini-2.0-flash"-style ids (passed through), the "gemini:"/"google:"
    prefix, and tolerates Claude families ("haiku"->flash, others->pro).
    """
    if not model_name:
        return "gemini-2.5-flash"
    s = model_name.strip()
    low = s.lower()
    if low.startswith(("gemini:", "google:")):
        s = s.split(":", 1)[1]
        low = s.lower()
    if low.startswith("gemini-"):
        return s
    if "flash" in low or "haiku" in low:
        return "gemini-2.5-flash"
    return "gemini-2.0-pro"


def to_codex_model(model_name: Optional[str]) -> str:
    """Map a configured model string to a Codex CLI model id.

    Accepts "gpt-*"/"o*" ids (passed through) and the "codex:"/"openai:" prefix.

    Returns "" (empty) to mean "let codex use its config.toml default model".
    This is the safest default because ChatGPT-account logins only allow the
    model(s) your plan provides -- e.g. "gpt-5"/"gpt-5-codex" are rejected, while
    the configured "gpt-5.5" works. Set CODEX_DEFAULT_MODEL to force one.
    """
    default = os.getenv("CODEX_DEFAULT_MODEL", "")
    if not model_name:
        return default
    s = model_name.strip()
    low = s.lower()
    if low in {"codex", "openai"}:
        return default
    if low.startswith(("codex:", "openai:")):
        s = s.split(":", 1)[1]
        low = s.lower()
    if low.startswith(("gpt", "o1", "o3", "o4")):
        return s
    return default


# Instructions appended to the system prompt when tools are bound, telling the
# model to respond by selecting a tool through the structured output envelope.
_TOOL_PROTOCOL = """

## Tool selection protocol
You do NOT execute tools yourself. Respond with ONLY a single JSON object and no
other text whatsoever -- no markdown fences, no explanation, no preamble. The object
MUST have a "tool_calls" array where each element is
{{"name": <one of the tool names below>, "arguments": <object matching that tool's input schema>}}.

Example of the exact required format:
{{"tool_calls": [{{"name": "<tool_name>", "arguments": {{"some_arg": "value"}}}}]}}

Available tools:
{tool_catalog}
"""


def _tool_catalog(openai_tools: list[dict]) -> str:
    lines = []
    for t in openai_tools:
        fn = t["function"]
        params = json.dumps(fn.get("parameters", {"type": "object", "properties": {}}))
        lines.append(
            f"- {fn['name']}: {fn.get('description', '').strip()}\n  input schema: {params}"
        )
    return "\n".join(lines)


def _tool_envelope_schema(openai_tools: list[dict]) -> dict:
    """JSON schema forcing the model to select one or more bound tools."""
    tool_names = [t["function"]["name"] for t in openai_tools]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool_calls": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "enum": tool_names},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                },
            }
        },
        "required": ["tool_calls"],
    }


def _envelope_to_tool_calls(data: dict) -> list[dict]:
    """Convert a parsed tool-selection envelope into LangChain tool_calls."""
    tool_calls = []
    for call in (data or {}).get("tool_calls", []):
        if not isinstance(call, dict) or "name" not in call:
            continue
        tool_calls.append(
            {
                "name": call["name"],
                "args": call.get("arguments") or {},
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "tool_call",
            }
        )
    return tool_calls


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort extraction of a single JSON object from CLI text output.

    Handles markdown fences and surrounding prose by scanning for the first
    balanced ``{...}`` block. Returns None if nothing parses.
    """
    if not text:
        return None
    stripped = text.strip()
    # Fast path: whole output is JSON.
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # Strip markdown code fences if present.
    if "```" in stripped:
        import re

        for block in re.findall(r"```(?:json)?\s*(.*?)```", stripped, re.S):
            try:
                obj = json.loads(block.strip())
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    # Scan for the first balanced top-level object.
    start = stripped.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            c = stripped[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
        start = stripped.find("{", start + 1)
    return None


def _loop_handles_subprocess() -> bool:
    """Whether the running event loop can spawn subprocesses.

    On Windows only the ProactorEventLoop supports subprocesses. ``langgraph dev``
    (uvicorn) runs on a SelectorEventLoop on Windows, under which spawning the
    agent CLIs would fail -- so callers offload to a Proactor-loop worker thread.
    """
    if sys.platform != "win32":
        return True
    try:
        return isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop)
    except RuntimeError:
        return True


async def _offload_subprocess(make_coro: Callable[[], Any]) -> Any:
    """Await a subprocess-spawning coroutine, on a loop that supports subprocesses.

    If the current loop can spawn subprocesses (POSIX, or a Windows Proactor loop)
    we await directly. Otherwise (Windows SelectorEventLoop under ``langgraph dev``)
    we run the work in a worker thread that owns its own ProactorEventLoop.
    """
    if _loop_handles_subprocess():
        return await make_coro()
    box: dict = {}

    def runner():
        loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(loop)
            box["value"] = loop.run_until_complete(make_coro())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    await asyncio.to_thread(runner)
    return box.get("value")


async def _run_cli(
    cmd: list[str],
    env: Optional[dict] = None,
    stdin: Optional[str] = None,
    timeout: int = 600,
    cwd: Optional[str] = None,
) -> str:
    """Run a CLI subprocess and return stdout, raising on failure/timeout.

    Resolves the binary via PATH and, on Windows, runs ``.cmd``/``.bat`` shims
    (e.g. npm-installed ``gemini.cmd``) through ``cmd /c`` so they execute. Runs
    in a neutral empty cwd by default so agent CLIs don't load project context.
    Uses a blocking ``subprocess.run`` offloaded to a thread, so it works under
    any event loop (including the Windows SelectorEventLoop used by langgraph dev).
    """
    import shutil
    import subprocess

    resolved = shutil.which(cmd[0]) or cmd[0]
    argv = [resolved, *cmd[1:]]
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]

    run_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": cwd or _cli_cwd(),
        "timeout": timeout,
    }
    if stdin is None:
        # Close stdin so CLIs that "read additional input from stdin" (codex exec)
        # don't block forever.
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = stdin.encode()

    try:
        proc = await asyncio.to_thread(subprocess.run, argv, **run_kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"CLI timed out after {timeout}s: {cmd[0]}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="replace")[:800]
        raise RuntimeError(f"CLI {cmd[0]} failed (exit {proc.returncode}): {detail}")
    return (proc.stdout or b"").decode(errors="replace")


def _render_messages(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    """Split messages into (system_prompt, prompt transcript).

    The Agent SDK takes a single ``prompt`` string plus a ``system_prompt``. We
    concatenate all SystemMessages into the system prompt and render the rest of
    the conversation as a labelled transcript. Prior assistant tool calls and
    tool results are flattened to text (the graph owns real tool execution).
    """
    system_parts: list[str] = []
    convo: list[str] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            system_parts.append(_content_str(m.content))
        elif isinstance(m, HumanMessage):
            convo.append(f"[USER]\n{_content_str(m.content)}")
        elif isinstance(m, AIMessage):
            text = _content_str(m.content)
            if getattr(m, "tool_calls", None):
                calls = "; ".join(
                    f"{tc['name']}({json.dumps(tc.get('args', {}))})"
                    for tc in m.tool_calls
                )
                text = (text + f"\n[assistant called tools: {calls}]").strip()
            convo.append(f"[ASSISTANT]\n{text}")
        elif isinstance(m, ToolMessage):
            convo.append(f"[TOOL RESULT name={m.name}]\n{_content_str(m.content)}")
        else:  # pragma: no cover - defensive
            convo.append(_content_str(getattr(m, "content", str(m))))
    prompt = "\n\n".join(p for p in convo if p).strip() or " "
    return "\n\n".join(p for p in system_parts if p).strip(), prompt


def _content_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", json.dumps(block)))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


class ClaudeAgentChat(BaseChatModel):
    """A ``BaseChatModel`` backed by the Claude Agent SDK (Claude Code)."""

    model: str = "sonnet"
    max_tokens: Optional[int] = None  # accepted for compatibility; SDK has no equivalent
    subscription: bool = True
    max_turns: Optional[int] = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "claude-agent-sdk"

    # -- tool binding -------------------------------------------------------
    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):
        """Bind tools so the model EMITS tool calls for the graph to execute.

        ``tool_choice`` and other structured-output kwargs from the base
        ``with_structured_output`` are accepted and ignored.
        """
        openai_tools = [convert_to_openai_tool(t) for t in tools]
        return self.bind(claude_tools=openai_tools)

    # -- generation ---------------------------------------------------------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError(
            "ClaudeAgentChat is async-only; use ainvoke()/astream()."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        openai_tools: list[dict] = kwargs.get("claude_tools") or []
        system_prompt, prompt = _render_messages(messages)

        output_format = None
        if openai_tools:
            system_prompt = (system_prompt + _TOOL_PROTOCOL.format(
                tool_catalog=_tool_catalog(openai_tools)
            )).strip()
            output_format = {
                "type": "json_schema",
                "schema": _tool_envelope_schema(openai_tools),
            }

        message = await self._run_query(prompt, system_prompt, output_format)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _run_query(
        self,
        prompt: str,
        system_prompt: str,
        output_format: Optional[dict],
    ) -> AIMessage:
        # In subscription mode, ensure the subprocess does not see an API key
        # (its presence forces pay-per-token API billing).
        if self.subscription:
            os.environ.pop("ANTHROPIC_API_KEY", None)

        options = cas.ClaudeAgentOptions(
            allowed_tools=[],
            disallowed_tools=[],
            mcp_servers={},
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt or None,
            output_format=output_format,
            model=self.model,
        )

        async def _consume() -> dict:
            data = {"text": "", "structured": None, "result_text": None, "usage": None, "is_error": False}

            def _handle(msg) -> None:
                if isinstance(msg, cas.AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, cas.TextBlock):
                            data["text"] += block.text
                elif isinstance(msg, cas.ResultMessage):
                    data["structured"] = msg.structured_output
                    data["result_text"] = msg.result
                    data["usage"] = msg.usage
                    data["is_error"] = bool(msg.is_error)

            await _drain_query_with_timeout(prompt, options, _handle, _SDK_TIMEOUT_S)
            # Raise inside the retried unit (not after it) so an error result the SDK
            # reports *gracefully* (is_error flag, no exception) is still classified by
            # _is_transient_sdk_error and retried when transient.
            if data["is_error"]:
                # Plain (not !r) interpolation so a transient result string ("success",
                # "overloaded", ...) is still matched by _is_transient_sdk_error.
                raise RuntimeError(
                    f"Claude Agent SDK returned an error result: {data['result_text']}"
                )
            return data

        async def _attempt() -> dict:
            async with _semaphore():
                return await _offload_subprocess(_consume)

        # Retry transient/spurious failures (hung subprocess -> timeout, overload, the
        # contradictory "error result: success" envelope) instead of failing the turn.
        data = await _run_with_retry(
            _attempt, max_attempts=_SDK_MAX_ATTEMPTS, backoff_s=_SDK_RETRY_BACKOFF_S
        )
        text = data["text"]
        structured = data["structured"]
        result_text = data["result_text"]
        usage = data["usage"]

        response_metadata = {"model": self.model}
        if usage is not None:
            response_metadata["usage"] = usage

        if output_format is not None and structured is not None:
            return AIMessage(
                content="",
                tool_calls=_envelope_to_tool_calls(structured),
                response_metadata=response_metadata,
            )

        return AIMessage(
            content=text or (result_text or ""),
            response_metadata=response_metadata,
        )


def _combine_system_prompt(system_prompt: str, prompt: str) -> str:
    if not system_prompt:
        return prompt
    return f"<system>\n{system_prompt}\n</system>\n\n{prompt}"


class _CLIJsonChat(BaseChatModel):
    """Shared base for CLI-driven LLM backends without the Claude SDK.

    Subclasses (Gemini CLI, Codex CLI) shell out to a local agent CLI in
    non-interactive mode. Structured output and tool-call emission use the JSON
    tool-selection envelope, which we parse back into ``AIMessage.tool_calls`` --
    the same contract as the Claude backend, so ``with_structured_output`` /
    ``bind_tools`` work uniformly. Codex enforces the schema natively
    (``--output-schema``); Gemini coerces it via the prompt.
    """

    model: str = ""
    max_tokens: Optional[int] = None
    subscription: bool = True
    timeout_s: int = int(os.getenv("CLI_BACKEND_TIMEOUT", "600"))

    model_config = {"arbitrary_types_allowed": True}

    _backend_name: str = "cli"

    # Subclass hook: run the CLI for one turn and return (text, parsed_json).
    async def _backend_generate(
        self, system_prompt: str, prompt: str, schema: Optional[dict]
    ) -> tuple[str, Optional[dict]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _subprocess_env(self) -> dict:
        return dict(os.environ)

    async def _invoke(self, cmd: list[str], stdin: Optional[str] = None) -> str:
        async with _semaphore():
            return await _run_cli(
                cmd, env=self._subprocess_env(), stdin=stdin, timeout=self.timeout_s
            )

    # BaseChatModel plumbing ----------------------------------------------
    @property
    def _llm_type(self) -> str:
        return self._backend_name

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):
        openai_tools = [convert_to_openai_tool(t) for t in tools]
        return self.bind(claude_tools=openai_tools)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError(f"{self._backend_name} is async-only; use ainvoke().")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        openai_tools: list[dict] = kwargs.get("claude_tools") or []
        system_prompt, prompt = _render_messages(messages)

        if openai_tools:
            # Tool/structured call: coerce the JSON envelope. CLI models without
            # native schema enforcement occasionally answer in prose instead of
            # JSON, so retry a few times until a valid envelope parses (the graph
            # treats empty tool_calls as "done", which would misbehave silently).
            system_prompt = (system_prompt + _TOOL_PROTOCOL.format(
                tool_catalog=_tool_catalog(openai_tools)
            )).strip()
            schema = _tool_envelope_schema(openai_tools)
            attempts = max(1, int(os.getenv("CLI_TOOL_RETRIES", "3")))
            tool_calls: list[dict] = []
            last_text = ""
            for _ in range(attempts):
                last_text, parsed = await self._backend_generate(system_prompt, prompt, schema)
                data = parsed if parsed is not None else (_extract_json(last_text) or {})
                tool_calls = _envelope_to_tool_calls(data)
                if tool_calls:
                    break
            if not tool_calls:
                # Don't return an empty tool_calls list: the graph reads that as "no
                # tool / done" and would silently end the phase. Raise so retry / the
                # supervisor's per-unit gather surfaces it as a real, logged failure.
                logger.error(
                    "%s produced no parseable tool-selection envelope after %d attempts; "
                    "last raw output (truncated): %.500s",
                    self._backend_name, attempts, last_text,
                )
                raise ValueError(
                    f"{self._backend_name} did not emit a valid tool-call envelope after "
                    f"{attempts} attempts (model not honoring the tool protocol)."
                )
            message = AIMessage(
                content="",
                tool_calls=tool_calls,
                response_metadata={"model": self.model},
            )
        else:
            text, _ = await self._backend_generate(system_prompt, prompt, None)
            if not text.strip():
                logger.warning("%s returned empty content for a plain generation", self._backend_name)
            message = AIMessage(
                content=text.strip(),
                response_metadata={"model": self.model},
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class GeminiCLIChat(_CLIJsonChat):
    """LLM backend driven by Google's ``agy`` CLI (replacement for ``gemini``).

    Gemini has no schema-enforcement flag, so structured output is coerced by
    appending the envelope schema to the prompt and parsing the JSON back.
    """

    _backend_name = "gemini-cli"

    def _subprocess_env(self) -> dict:
        env = dict(os.environ)
        # The standard gemini CLI refuses headless runs in an "untrusted" dir; we run it
        # in a neutral temp cwd, so trust the workspace explicitly (env, not a flag).
        env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
        if self.subscription:
            # Force OAuth/free-tier login instead of API-key (paid) billing.
            for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
                env.pop(key, None)
        return env

    async def _backend_generate(self, system_prompt, prompt, schema):
        if schema is not None:
            prompt = (
                prompt
                + "\n\nReturn ONLY a single JSON object matching this schema "
                "(no markdown fences, no commentary):\n"
                + json.dumps(schema)
            )
        full = _combine_system_prompt(system_prompt, prompt)
        bin_ = os.getenv("GEMINI_CLI_BIN", "gemini")
        # agy uses --dangerously-skip-permissions to approve tools non-interactively.
        extra = os.getenv("GEMINI_CLI_ARGS", "").split()  # standard gemini CLI: no agy flags
        # Pass the prompt via STDIN to survive the Windows cmd /c shim.
        cmd = [bin_, "--model", self.model, *extra]
        raw = await self._invoke(cmd, stdin=full)
        # agy occasionally appends a "### Summary" section to prose; strip it.
        if "### Summary" in raw:
            raw = raw.split("### Summary")[0].strip()
        return raw, None


class CodexCLIChat(_CLIJsonChat):
    """LLM backend driven by OpenAI's ``codex`` CLI (ChatGPT login).

    Uses ``--output-last-message`` to capture just the final agent message
    (codex exec otherwise prints a header + transcript). Structured output /
    tool selection is coerced via the prompt and parsed back: codex's
    ``--output-schema`` enforces OpenAI *strict* JSON-schema, which cannot
    express the tool envelope (heterogeneous per-tool ``arguments`` and
    ``minItems`` are disallowed in strict mode), so we don't use it.

    Notes: ``gpt-5``/``gpt-5-codex`` are rejected on ChatGPT-account logins;
    by default no ``-m`` is passed so codex uses its config.toml model.
    """

    _backend_name = "codex-cli"

    def _subprocess_env(self) -> dict:
        env = dict(os.environ)
        if self.subscription:
            # Force ChatGPT login instead of API-key (paid) billing.
            env.pop("OPENAI_API_KEY", None)
        return env

    async def _backend_generate(self, system_prompt, prompt, schema):
        import tempfile

        if schema is not None:
            prompt = (
                prompt
                + "\n\nReturn ONLY a single JSON object matching this schema "
                "(no markdown fences, no commentary):\n"
                + json.dumps(schema)
            )
        full = _combine_system_prompt(system_prompt, prompt)
        bin_ = os.getenv("CODEX_CLI_BIN", "codex")
        # --dangerously-bypass-approvals-and-sandbox for non-interactive tool auto-approval.
        extra = os.getenv("CODEX_CLI_ARGS", "--skip-git-repo-check --dangerously-bypass-approvals-and-sandbox").split()

        out_fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="codex_out_")
        os.close(out_fd)
        try:
            model_args = ["-m", self.model] if self.model else []
            # Pass '-' to read the prompt from STDIN to avoid argument list length limits.
            cmd = [bin_, "exec", *model_args, *extra,
                   "--output-last-message", out_path, "-"]
            await self._invoke(cmd, stdin=full)
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                content = ""
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        parsed = _extract_json(content) if schema is not None else None
        return content, parsed


async def run_search_agent(
    queries: Sequence[str],
    model: str = "haiku",
    max_results: int = 5,
    max_turns: int = 8,
) -> str:
    """Run Claude Code's native web search over a set of queries.

    Spins up a Claude Agent SDK session with the built-in ``WebSearch`` and
    ``WebFetch`` tools ENABLED, lets it actually search and read the web, and
    returns its compiled findings (with source URLs). This is how the researcher
    performs real search instead of answering from pretrained knowledge.
    """
    if use_subscription():
        os.environ.pop("ANTHROPIC_API_KEY", None)

    prompt = (
        "Research the following queries using the WebSearch tool (use WebFetch to "
        "read a promising page when helpful). For each query, find current, factual "
        "information and compile concise notes grouped by query. Always include the "
        "source URL for every fact.\n\nQueries:\n"
        + "\n".join(f"- {q}" for q in queries)
    )
    options = cas.ClaudeAgentOptions(
        allowed_tools=["WebSearch", "WebFetch"],
        disallowed_tools=[],
        mcp_servers={},
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        system_prompt=(
            "You are a meticulous web research assistant. Use WebSearch to find "
            "current, factual information and always cite source URLs. Be concise."
        ),
        model=model,
    )

    async def _consume() -> dict:
        data = {"text": "", "result_text": None, "is_error": False}

        def _handle(msg) -> None:
            if isinstance(msg, cas.AssistantMessage):
                for block in msg.content:
                    if isinstance(block, cas.TextBlock):
                        data["text"] += block.text
            elif isinstance(msg, cas.ResultMessage):
                data["result_text"] = msg.result
                data["is_error"] = bool(msg.is_error)

        await _drain_query_with_timeout(prompt, options, _handle, _SDK_TIMEOUT_S)
        return data

    async def _attempt() -> dict:
        async with _semaphore():
            return await _offload_subprocess(_consume)

    # A hung or overloaded web-search subprocess is the most likely stall point;
    # bound it with a timeout and retry transient failures rather than wedging the run.
    data = await _run_with_retry(
        _attempt, max_attempts=_SDK_MAX_ATTEMPTS, backoff_s=_SDK_RETRY_BACKOFF_S
    )
    text, result_text, is_error = data["text"], data["result_text"], data["is_error"]

    if is_error and not text:
        logger.error("Claude Code web search returned an error result: %r", result_text)
        return f"Error performing Claude Code web search: {result_text!r}"
    return text or result_text or "No search results found."


def _search_prompt(queries: Sequence[str], tool_hint: str) -> str:
    return (
        f"{tool_hint} For each query, find current, factual information and compile "
        "concise notes grouped by query. Always include the source URL for every "
        "fact.\n\nQueries:\n" + "\n".join(f"- {q}" for q in queries)
    )


async def run_gemini_search(
    queries: Sequence[str],
    model: str = "gemini-2.5-flash",
    max_results: int = 5,
) -> str:
    """Web search via the Gemini CLI's built-in Google Search grounding."""
    bin_ = os.getenv("GEMINI_CLI_BIN", "gemini")
    extra = os.getenv("GEMINI_SEARCH_ARGS", "").split()  # standard gemini CLI: no agy flags
    prompt = _search_prompt(queries, "Use Google Search to research the following queries.")
    # Prompt via STDIN (see GeminiCLIChat) to survive the Windows cmd /c shim.
    cmd = [bin_, "--model", model, *extra]
    env = dict(os.environ)
    env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")  # headless trust gate (neutral cwd)
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        env.pop(key, None)
    async with _semaphore():
        try:
            return await _run_cli(
                cmd, env=env, stdin=prompt, timeout=int(os.getenv("CLI_BACKEND_TIMEOUT", "600"))
            )
        except Exception as e:  # surface as tool output rather than crashing the loop
            logger.error("Gemini web search failed: %s", e, exc_info=True)
            return f"Error performing Gemini web search: {e}"


async def run_codex_search(
    queries: Sequence[str],
    model: str = "",
    max_results: int = 5,
) -> str:
    """Web search via the Codex CLI's web-search tool (``features.web_search_request``).

    Codex attempts a WebSocket transport first and auto-falls-back to HTTPS when
    WebSockets are blocked; the final agent message is captured cleanly via
    ``--output-last-message`` (codex exec otherwise prints a header + transcript).
    """
    import tempfile

    bin_ = os.getenv("CODEX_CLI_BIN", "codex")
    # --search is a global flag; --skip-git-repo-check and --dangerously-bypass-approvals-and-sandbox are subcommand flags.
    extra_global = ["--search"]
    extra_exec = os.getenv(
        "CODEX_SEARCH_ARGS", "--skip-git-repo-check --dangerously-bypass-approvals-and-sandbox"
    ).split()
    prompt = _search_prompt(queries, "Use web search to research the following queries.")
    out_fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="codex_search_")
    os.close(out_fd)
    model_args = ["-m", model] if model else []
    # Pass '-' to read the prompt from STDIN to avoid argument list length limits.
    cmd = [bin_, *extra_global, "exec", *model_args, *extra_exec, "--output-last-message", out_path, "-"]
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)

    async def _attempt():
        async with _semaphore():
            await _run_cli(cmd, env=env, stdin=prompt, timeout=int(os.getenv("CLI_BACKEND_TIMEOUT", "600")))

    try:
        await _run_with_retry(_attempt, max_attempts=_SDK_MAX_ATTEMPTS, backoff_s=_SDK_RETRY_BACKOFF_S)
        with open(out_path, "r", encoding="utf-8") as f:
            return f.read().strip() or "No search results found."
    except Exception as e:
        logger.error("Codex web search failed: %s", e, exc_info=True)
        return f"Error performing Codex web search: {e}"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# Map a provider-prefixed model string to a concrete backend + bare model id.
_BACKEND_PREFIXES = {
    "claude": "claude",
    "anthropic": "claude",
    "gemini": "gemini",
    "google": "gemini",
    "codex": "codex",
    "openai": "codex",
}


def parse_backend(model_string: Optional[str]) -> tuple[str, str]:
    """Resolve (backend, model) from a model string.

    A provider prefix selects the backend per role, e.g. "gemini:2.5-pro",
    "codex:gpt-5", "claude:opus". Bare values are routed by keyword, defaulting
    to the Claude backend.
    """
    s = (model_string or "").strip()
    low = s.lower()
    if ":" in low:
        prefix = low.split(":", 1)[0]
        if prefix in _BACKEND_PREFIXES:
            return _BACKEND_PREFIXES[prefix], s.split(":", 1)[1]
    if low in {"opus", "sonnet", "haiku"} or low.startswith("claude"):
        return "claude", s
    if low == "gemini" or low.startswith("gemini"):
        return "gemini", s
    if low == "codex" or low.startswith(("gpt", "o1", "o3", "o4")):
        return "codex", s
    return "claude", s


def build_chat_model(model_string: Optional[str], max_tokens: Optional[int] = None) -> BaseChatModel:
    """Construct the right CLI-backed chat model for a (possibly prefixed) string."""
    backend, model = parse_backend(model_string)
    subscription = use_subscription()
    if backend == "gemini":
        return GeminiCLIChat(model=to_gemini_model(model), max_tokens=max_tokens, subscription=subscription)
    if backend == "codex":
        return CodexCLIChat(model=to_codex_model(model), max_tokens=max_tokens, subscription=subscription)
    return ClaudeAgentChat(model=to_claude_model(model), max_tokens=max_tokens, subscription=subscription)


class configurable_claude_model(Runnable):
    """Drop-in replacement for the ``init_chat_model`` configurable model.

    Records declarative operations (``with_structured_output`` / ``bind_tools`` /
    ``with_retry``) and the flat ``with_config`` model settings, then materialises
    a real :class:`ClaudeAgentChat` and replays the operations at invoke time --
    mirroring how the LangChain ``_ConfigurableModel`` behaves, but for the
    Claude Agent SDK backend.
    """

    def __init__(
        self,
        default_config: Optional[dict] = None,
        queue: Optional[list] = None,
    ) -> None:
        self._default_config = dict(default_config or {})
        self._queue = list(queue or [])

    # -- declarative chaining (all return new copies) -----------------------
    def _copy(self, default_config=None, queue=None) -> "configurable_claude_model":
        return configurable_claude_model(
            default_config if default_config is not None else self._default_config,
            queue if queue is not None else self._queue,
        )

    def with_config(self, config: Optional[dict] = None, **kwargs: Any):
        merged = dict(self._default_config)
        source = dict(config or {})
        source.update(kwargs)
        # Capture the flat model settings the codebase passes in.
        for key in ("model", "max_tokens", "api_key", "model_chain", "stage", "run_key"):
            if key in source:
                merged[key] = source[key]
        configurable = source.get("configurable") or {}
        for key in ("model", "max_tokens", "api_key", "model_chain", "stage", "run_key"):
            if key in configurable:
                merged[key] = configurable[key]
        return self._copy(default_config=merged)

    def with_structured_output(self, *args: Any, **kwargs: Any):
        return self._copy(queue=self._queue + [("with_structured_output", args, kwargs)])

    def bind_tools(self, *args: Any, **kwargs: Any):
        return self._copy(queue=self._queue + [("bind_tools", args, kwargs)])

    def with_retry(self, *args: Any, **kwargs: Any):
        return self._copy(queue=self._queue + [("with_retry", args, kwargs)])

    # -- materialisation ----------------------------------------------------
    def _materialize(self, config: Optional[RunnableConfig] = None,
                     model_override: Optional[str] = None) -> Runnable:
        cfg = dict(self._default_config)
        if config:
            configurable = config.get("configurable") or {}
            for key in ("model", "max_tokens", "api_key"):
                if key in configurable:
                    cfg[key] = configurable[key]
        model_string = model_override if model_override is not None else cfg.get("model")
        model: Runnable = build_chat_model(model_string, cfg.get("max_tokens"))
        for name, args, kwargs in self._queue:
            model = getattr(model, name)(*args, **kwargs)
        return model

    def _resolve_chain(self, config: Optional[RunnableConfig] = None) -> "tuple[list[str], str, str | None]":
        """The model chain to try (primary first), the stage label for logging, and the run key.

        Returns a 3-tuple: (chain, stage, run_key).  ``run_key`` is an explicit
        thread-scoped key for the registry-backed tracker (set via ``with_config``
        or the ``configurable`` sub-dict); it is ``None`` when no key was provided.
        """
        cfg = dict(self._default_config)
        if config:
            configurable = config.get("configurable") or {}
            for key in ("model", "model_chain", "stage", "run_key"):
                if key in configurable:
                    cfg[key] = configurable[key]
        chain = cfg.get("model_chain") or ([cfg["model"]] if cfg.get("model") else [])
        chain = [m for m in chain if m]
        return chain, (cfg.get("stage") or "model"), cfg.get("run_key")

    # -- Runnable interface -------------------------------------------------
    async def ainvoke(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        from open_deep_research.failover import backend_of, classify_error, get_tracker, reason_for

        chain, stage, run_key = self._resolve_chain(config)
        if len(chain) <= 1:
            model_override = chain[0] if chain else None
            return await self._materialize(config, model_override=model_override).ainvoke(
                input, config, **kwargs)

        if run_key is None:
            try:
                from langchain_core.runnables.config import ensure_config
                ambient = ensure_config(config)
                run_key = (ambient.get("configurable") or {}).get("thread_id")
            except Exception:  # noqa: BLE001 - config propagation is best-effort
                run_key = None

        tracker = get_tracker(run_key)
        # Skip models already marked down this run; if all are down, still try the last
        # so a real error surfaces rather than silently returning nothing.
        available = tracker.available_chain(chain) or chain[-1:]
        last_exc: Optional[BaseException] = None
        for idx, model_string in enumerate(available):
            try:
                return await self._materialize(config, model_override=model_string).ainvoke(
                    input, config, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below when the chain is exhausted
                last_exc = exc
                kind = classify_error(exc)
                if kind == "backend_fatal":
                    bk = backend_of(model_string)
                    tracker.mark_backend_down(bk)
                    from open_deep_research.failover import record_backend_exhausted
                    record_backend_exhausted(bk)
                elif kind == "model_fatal":
                    tracker.mark_down(model_string)
                if idx >= len(available) - 1:
                    raise  # nothing left to fail over to
                next_model = available[idx + 1]
                reason = reason_for(exc, kind)
                tracker.record_failover(stage, model_string, next_model, reason)
                logger.warning("failover[%s]: %s unavailable (%s) -> %s",
                               stage, model_string, reason, next_model)
        assert last_exc is not None  # unreachable: the loop raises on its last attempt
        raise last_exc

    def invoke(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        chain, _, _ = self._resolve_chain(config)
        if len(chain) > 1:
            raise RuntimeError(
                "sync invoke() does not support model failover; use ainvoke() for a "
                f"multi-element chain ({chain})")
        return self._materialize(config).invoke(input, config, **kwargs)

    async def astream(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        from open_deep_research.failover import get_tracker

        # astream honours already-marked-down models but does not itself fail over or
        # mark-down on a mid-stream error: a partial stream can't be replayed. Full
        # reactive failover lives in ainvoke (the path every graph stage uses).
        chain, _, _ = self._resolve_chain(config)
        model_override = None
        if chain:
            available = get_tracker().available_chain(chain) or chain
            model_override = available[0]
        async for chunk in self._materialize(config, model_override=model_override).astream(
                input, config, **kwargs):
            yield chunk

