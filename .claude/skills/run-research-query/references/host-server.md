# Host langgraph server: auth, networking, and the SQLite KB

Read this when driving the host `langgraph dev` server (Path A), especially before
touching auth, or when inspecting what the run stored.

## Why WSL can't reach the host server directly

`langgraph dev` binds `127.0.0.1:2024` on the **Windows host**. WSL2 in **NAT mode**
(a real gateway like `172.28.x.1` on a separate subnet) cannot reach a host service
bound to `127.0.0.1`. Confirm NAT mode with `ip route show default`. The only reliable
way in from WSL is to run a **Windows** process (`curl.exe`, `python.exe`), which lives
in the host's network namespace. (Windows 11 *mirrored* networking would let plain
`localhost` work, but this setup is NAT.)

Host-IP probes (`host.docker.internal`, the gateway IP, the LAN IP) all fail because
the server is bound to host-`127.0.0.1` only — so don't waste time on them; go straight
to `curl.exe`.

## The auth handler

`langgraph.json` registers `src/security/auth.py:auth`, a Supabase JWT check that runs
on every request **except** health (`/ok`). With Supabase creds empty (subscription
dev setup), scripted calls to `/assistants/search`, `/runs/*`, etc. return
`401 "Authorization header missing"`. The browser Studio UI still works because it
authenticates as a `StudioUser` that bypasses the custom handler — scripts have no such
pass.

**Never** forge or brute-force auth headers to get past the 401 — that's defeating a
security control and the harness will (correctly) block it.

To run scripted against the server, with the user's explicit consent:
1. Remove the `auth` block from `langgraph.json` (it's git-tracked: revert with
   `git checkout langgraph.json`).
2. The user restarts the host `langgraph dev` (a `langgraph.json` change does NOT
   hot-reload; it needs a restart).
3. Verify it's open: `/assistants/search` now returns `200`.
4. Run the query.
5. **Restore the `auth` block and have the user restart again** so the server is
   secured. Leaving it open is a real vulnerability.

Note: `langgraph dev` hot-reloads **Python** code edits — but edits made from WSL onto
`/mnt/c` do **not** trigger the Windows file-watcher, so code changes still need a
manual host restart to take effect.

## Inspecting the SQLite knowledge base

Completed runs persist to `research_results.db` (project root by default; precedence:
`database_path` config → `RESEARCH_DB_PATH` env → `research_results.db`). Query it
read-only from WSL (the file is on `/mnt/c`):

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:research_results.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,slug,name,run_count,length(current_report) rep FROM subjects"):
    print(dict(r))
PY
```

Tables:
- `subjects` — one accumulated **dossier** per subject (`current_report`, `sources`,
  `run_count`). Keyed by `slugify(name)`.
- `research_runs` — full history, one row per run (`topic`, `research_brief`,
  `final_report`, `raw_notes`, `sources`, `config`, `status`).
- `dossier_versions` — timestamped dossier snapshots (the subject's timeline).

**Health tells:** a `research_runs` row with `raw_notes` = `[]` (length 2 = the string
`"[]"`) means that run did no researcher fan-out — the report came from the final-report
model's own search. `status='answered_from_cache'` means it was a KB cache hit, not
fresh research.
