"""NVIDIA backend: OpenAI-compatible ChatOpenAI wired in behind an ``nvidia:`` prefix.

NVIDIA's hosted API speaks the OpenAI protocol, so the ``nvidia:`` backend returns a
real ``langchain_openai.ChatOpenAI`` pointed at NVIDIA's ``/v1`` endpoint -- giving the
graph native ``with_structured_output`` / ``bind_tools`` / failover with no new dependency.
"""
import pytest

from open_deep_research import claude_agent_chat as cac
from open_deep_research.claude_agent_chat import (
    build_chat_model,
    parse_backend,
    to_nvidia_model,
)
from open_deep_research.failover import backend_of

_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


@pytest.fixture(autouse=True)
def _clear_nvidia_env(monkeypatch):
    """Hermetic tests: drop any NVIDIA_* config from the host env before each test."""
    for key in (
        "NVIDIA_API_KEY", "NVIDIA_BASE_URL", "NVIDIA_ENABLE_THINKING",
        "NVIDIA_REASONING_BUDGET", "NVIDIA_TEMPERATURE", "NVIDIA_TOP_P", "NVIDIA_EXTRA_BODY",
        "NVIDIA_TIMEOUT", "NVIDIA_MAX_RETRIES", "NVIDIA_RPM",
    ):
        monkeypatch.delenv(key, raising=False)
    cac._NVIDIA_RATE_LIMITERS.clear()  # hermetic: drop cached per-model limiters


# -- routing: prefix -> (backend, model) ------------------------------------
def test_parse_backend_nvidia_prefix():
    assert parse_backend(f"nvidia:{_MODEL}") == ("nvidia", _MODEL)


def test_parse_backend_bare_nvidia_vendor_id():
    # A bare NVIDIA vendor id (no explicit prefix) still routes to the nvidia backend.
    assert parse_backend(_MODEL) == ("nvidia", _MODEL)


def test_to_nvidia_model_strips_prefix_and_passes_through():
    assert to_nvidia_model(f"nvidia:{_MODEL}") == _MODEL
    assert to_nvidia_model(_MODEL) == _MODEL


def test_backend_of_nvidia():
    assert backend_of(f"nvidia:{_MODEL}") == "nvidia"


# -- materialisation: a configured ChatOpenAI -------------------------------
def test_build_chat_model_returns_configured_chatopenai(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("NVIDIA_BASE_URL", raising=False)
    monkeypatch.delenv("NVIDIA_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("NVIDIA_REASONING_BUDGET", raising=False)
    monkeypatch.delenv("NVIDIA_TEMPERATURE", raising=False)
    monkeypatch.delenv("NVIDIA_TOP_P", raising=False)

    model = build_chat_model(f"nvidia:{_MODEL}", max_tokens=12345)

    from langchain_openai import ChatOpenAI
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == _MODEL
    assert model.openai_api_base == "https://integrate.api.nvidia.com/v1"
    assert model.max_tokens == 12345
    assert model.temperature == 1.0
    assert model.top_p == 0.95
    # Reasoning/thinking forwarded via OpenAI extra_body (NVIDIA-specific knobs).
    assert model.extra_body["chat_template_kwargs"] == {"enable_thinking": True}
    assert model.extra_body["reasoning_budget"] == 16384
    assert model.openai_api_key.get_secret_value() == "nvapi-test-key"
    # Bounded per-request timeout; client retries transient 429 throttles with backoff.
    assert model.request_timeout == 120.0
    assert model.max_retries == 2


def test_rate_limiter_attached_and_shared_per_model(monkeypatch):
    # Per-model client-side pacing under NVIDIA's ~40 RPM free-tier ceiling: every build for
    # the same model id must SHARE one limiter so the budget is enforced across the process.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    from langchain_core.rate_limiters import InMemoryRateLimiter

    a = build_chat_model(f"nvidia:{_MODEL}")
    b = build_chat_model(f"nvidia:{_MODEL}")
    c = build_chat_model(f"nvidia:{_MINIMAX}")
    assert isinstance(a.rate_limiter, InMemoryRateLimiter)
    assert a.rate_limiter is b.rate_limiter          # same model -> shared budget
    assert a.rate_limiter is not c.rate_limiter      # different model -> separate budget


def test_rate_limiter_rpm_env_controls_pacing(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_RPM", "60")
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.rate_limiter.requests_per_second == 1.0  # 60 RPM / 60


def test_rate_limiter_disabled_when_rpm_zero(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_RPM", "0")
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.rate_limiter is None


def test_timeout_and_retries_are_env_overridable(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_TIMEOUT", "45")
    monkeypatch.setenv("NVIDIA_MAX_RETRIES", "2")
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.request_timeout == 45.0
    assert model.max_retries == 2


def test_build_chat_model_honours_env_overrides(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("NVIDIA_TEMPERATURE", "0.2")
    monkeypatch.setenv("NVIDIA_TOP_P", "0.5")
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "false")

    model = build_chat_model(f"nvidia:{_MODEL}")

    assert model.openai_api_base == "https://example.test/v1"
    assert model.temperature == 0.2
    assert model.top_p == 0.5
    # Thinking forced off -> no NIM thinking knobs sent at all (clean OpenAI request).
    assert model.extra_body is None


def test_reasoning_budget_override_when_thinking_enabled(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "true")
    monkeypatch.setenv("NVIDIA_REASONING_BUDGET", "4096")
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.extra_body["chat_template_kwargs"] == {"enable_thinking": True}
    assert model.extra_body["reasoning_budget"] == 4096


# -- per-model thinking auto-detection (nemotron on, others off) -------------
_MINIMAX = "minimaxai/minimax-m3"
_MINIMAX_27 = "minimaxai/minimax-m2.7"
_KIMI = "moonshotai/kimi-k2.6"
_GLM = "z-ai/glm-5.1"
_DEEPSEEK_PRO = "deepseek-ai/deepseek-v4-pro"
_DEEPSEEK_FLASH = "deepseek-ai/deepseek-v4-flash"


@pytest.mark.parametrize("model_id", [_MINIMAX, _MINIMAX_27, _KIMI, _GLM, _DEEPSEEK_PRO])
def test_non_nemotron_sends_no_thinking_knobs_by_default(monkeypatch, model_id):
    # Non-nemotron models don't support NVIDIA's thinking extensions by default: with no
    # explicit NVIDIA_EXTRA_BODY / NVIDIA_ENABLE_THINKING, the builder must NOT forward
    # chat_template_kwargs/reasoning_budget -- a clean OpenAI request.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("NVIDIA_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("NVIDIA_EXTRA_BODY", raising=False)
    model = build_chat_model(f"nvidia:{model_id}", max_tokens=16384)
    assert model.model_name == model_id
    assert model.max_tokens == 16384
    assert model.extra_body is None


# -- NVIDIA_EXTRA_BODY: externalised per-dialect escape hatch ----------------
def test_extra_body_env_used_verbatim(monkeypatch):
    # DeepSeek-v4 uses a different 'thinking' dialect (thinking + reasoning_effort, not
    # enable_thinking/reasoning_budget). NVIDIA_EXTRA_BODY forwards it verbatim, externalised.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv(
        "NVIDIA_EXTRA_BODY",
        '{"chat_template_kwargs": {"thinking": true, "reasoning_effort": "high"}}',
    )
    model = build_chat_model(f"nvidia:{_DEEPSEEK_FLASH}", max_tokens=16384)
    assert model.extra_body == {
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "high"}
    }


def test_extra_body_env_overrides_nemotron_default(monkeypatch):
    # The raw JSON wins even for a nemotron id (it replaces the auto enable_thinking default).
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_EXTRA_BODY", '{"chat_template_kwargs": {"thinking": false}}')
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.extra_body == {"chat_template_kwargs": {"thinking": False}}


def test_extra_body_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_EXTRA_BODY", "{not valid json")
    with pytest.raises(RuntimeError, match="NVIDIA_EXTRA_BODY is not valid JSON"):
        build_chat_model(f"nvidia:{_DEEPSEEK_FLASH}")


def test_extra_body_non_object_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_EXTRA_BODY", '["not", "an", "object"]')
    with pytest.raises(RuntimeError, match="must be a JSON object"):
        build_chat_model(f"nvidia:{_DEEPSEEK_FLASH}")


def test_nemotron_sends_thinking_knobs_by_default(monkeypatch):
    # Counterpart: a nemotron reasoning model auto-enables thinking with no env set.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("NVIDIA_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("NVIDIA_REASONING_BUDGET", raising=False)
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.extra_body["chat_template_kwargs"] == {"enable_thinking": True}
    assert model.extra_body["reasoning_budget"] == 16384


def test_minimax_thinking_can_be_forced_on(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("NVIDIA_ENABLE_THINKING", "true")
    model = build_chat_model(f"nvidia:{_MINIMAX}")
    assert model.extra_body["chat_template_kwargs"] == {"enable_thinking": True}


def test_build_chat_model_requires_api_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        build_chat_model(f"nvidia:{_MODEL}")


def test_nvidia_backend_ignores_subscription_mode(monkeypatch):
    # NVIDIA is an API backend: it must authenticate via NVIDIA_API_KEY regardless of
    # whether the rest of the system is running in Claude-subscription mode.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setattr(cac, "use_subscription", lambda: True)
    model = build_chat_model(f"nvidia:{_MODEL}")
    assert model.openai_api_key.get_secret_value() == "nvapi-test-key"
