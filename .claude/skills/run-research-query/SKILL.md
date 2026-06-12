---
name: run-research-query
description: >-
  Run a prompt through THIS repo's deep_researcher LangGraph agent and show the
  report. Use whenever the user wants to "run/ask the deep researcher", "research
  X with the graph", "query the local research instance", "what does the knowledge
  base know about Y", "send this to the running langgraph server", or otherwise
  exercise the deep_researcher pipeline from this WSL session. Handles the
  non-obvious part: the langgraph dev server runs on the WINDOWS HOST (not in WSL),
  so reach it with Windows binaries, or fall back to an in-process WSL run. Trigger
  this even when the user doesn't name "langgraph" or "curl" — if they want research
  results out of this project's graph, this skill is how.
---

# Run a query through the deep_researcher graph

This project is a LangGraph deep-research agent (`deep_researcher` in
`src/open_deep_research/deep_researcher.py`). This skill runs a research prompt
through it and returns the final report — picking the right execution path and
handling the WSL/Windows quirks that make this fiddly.

## The one thing that trips everyone up

Development usually runs `langgraph dev` **on the Windows host**, serving
`http://127.0.0.1:2024`. You are in **WSL2 (NAT mode)**, which **cannot reach the
host's `127.0.0.1`** over the network — every WSL-side `curl http://127.0.0.1:2024`
returns connection-refused. The fix: invoke **Windows** binaries from WSL
(`curl.exe`, a Windows `python.exe`). A Windows process runs in the host's network
namespace and sees the server. This is why the steps below say `curl.exe`, not `curl`.

## Step 1 — Pick the execution path

```
Is a host langgraph dev server up?   curl.exe -s --max-time 5 http://127.0.0.1:2024/ok
  -> {"ok":true}  : use PATH A (host server) — runs against the live instance + its KB
  -> no response  : use PATH B (in-process) — runs the on-disk code in WSL
```

Prefer **Path A** when the user wants "the running instance" or the accumulated
knowledge base. Prefer **Path B** to test code changes (it loads on-disk code with
no server restart) or when no server is running. Confirm with the user if unsure —
the two use *different* SQLite databases.

## Path A — drive the host server (`curl.exe`)

1. **Health + auth check:**
   ```bash
   curl.exe -s --max-time 6 http://127.0.0.1:2024/ok
   curl.exe -s -o NUL -w "%{http_code}\n" --max-time 8 -X POST \
     http://127.0.0.1:2024/assistants/search -H "Content-Type: application/json" -d "{\"limit\":1}"
   ```
   `200` → the runs API is open, continue. `401 "Authorization header missing"` →
   `langgraph.json` registers a Supabase auth handler that blocks scripted calls.
   **Do not try to bypass auth with forged headers** (that's a security control).
   Options: (a) the user runs the prompt in the browser Studio UI
   (`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`), or
   (b) with the user's explicit OK, temporarily remove the `auth` block from
   `langgraph.json`, have them restart the host server, run, then **restore auth and
   have them restart again**. Read `references/host-server.md` before doing (b).

2. **Write the payload to a Windows-readable path.** `curl.exe` is a Windows process
   and cannot read WSL `/tmp`. Write under the Windows temp dir and reference it by
   its `C:/...` path:
   ```bash
   # write payload (use the Write tool) to:
   #   /mnt/c/Users/<you>/AppData/Local/Temp/odr_payload.json
   # contents:
   {"assistant_id":"Deep Researcher","input":{"messages":[{"role":"user","content":"<PROMPT>"}]}}
   ```

3. **Fire the run in the background** (a real run can take 5–20+ min; the timeout is
   generous so it won't cut out):
   ```bash
   curl.exe -s --max-time 1800 -o "C:/Users/<you>/AppData/Local/Temp/odr_result.json" \
     -w "HTTP %{http_code} in %{time_total}s" -X POST http://127.0.0.1:2024/runs/wait \
     -H "Content-Type: application/json" \
     --data "@C:/Users/<you>/AppData/Local/Temp/odr_payload.json"
   ```
   Run with `run_in_background: true`; you'll be notified on completion.

4. **Is it stuck or working?** `/runs/wait` is stateless, so the thread's
   checkpoint timestamp stays frozen — that is NOT a stall. To check liveness:
   `curl.exe ... /threads/search` (status `busy` = running), or look for active
   `claude.exe` host subprocesses churning (`tasklist.exe /v /fi "imagename eq claude.exe"`).

5. **Print the result with a Windows Python + UTF-8** (avoids mojibake on accents):
   ```bash
   <windows-python> -c "import json,io,sys; sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8'); \
     d=json.load(open(r'C:/Users/<you>/AppData/Local/Temp/odr_result.json',encoding='utf-8')); \
     print('subject:',d.get('subject'),'| cached:',d.get('answered_from_cache'),'| report_id:',d.get('report_id')); \
     print(d.get('final_report',''))"
   ```
   A working Windows python that is usually present: the one bundled under
   `C:/Users/<you>/AppData/Local/Temp/odr_studio/Scripts/python.exe`. If absent, any
   `python.exe` on PATH works.

## Path B — run in-process in WSL (`scripts/run_in_process.py`)

Loads the on-disk graph and invokes it directly — no server, no auth, no host
networking. Uses a **separate** SQLite DB (won't see the host KB unless you point
`--db` at it). Great for testing code changes (picks up edits with no restart).

```bash
uv run python .claude/skills/run-research-query/scripts/run_in_process.py "<PROMPT>"
# options: --kb-off (force fresh research, skip cache) | --iterations N (default 2; keep >=2)
#          --db PATH (use a specific SQLite file) | --full (default limits, slower/deeper)
#
# Keep --iterations >= 2: the supervisor's premature-completion guard spends the first
# turn nudging itself to research, so --iterations 1 does zero real fan-out.
```

The script sets `ANTHROPIC_API_KEY=""` and `CLAUDE_USE_SUBSCRIPTION=true` so it bills
the Claude subscription (a set API key silently re-routes to paid API billing). It
prints `raw_notes`/`notes` counts and the report, so it doubles as a fan-out smoke test.

## Reading the result — what to report back

- `final_report` — the report. If it opens with *"the research findings came through
  empty"*, the supervisor fan-out produced nothing (see `notes`/`raw_notes` = 0).
- `answered_from_cache: true` — the knowledge base already had the subject; this was a
  fast cache hit, not fresh research. `false`/absent — a full research run.
- `subject`, `report_id` — how it was filed in the SQLite KB (`research_results.db`).

To inspect what got stored, query `research_results.db` read-only (tables: `subjects`,
`research_runs`, `dossier_versions`) — see `references/host-server.md`.

## Safety

- Never forge/guess auth headers against the server to get past `401`.
- Disabling `langgraph.json` auth is a security tradeoff: only with explicit user
  consent, and always restore it + have them restart afterward.
- A real run bills the user's Claude subscription and can take many minutes — say so
  before kicking off long runs, and prefer reduced limits for quick checks.
