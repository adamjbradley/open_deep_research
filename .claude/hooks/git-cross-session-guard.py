#!/usr/bin/env python3
"""PreToolUse(Bash) guard against silent cross-session git collisions.

A branch / working-tree is shared state at the *directory* level: there is one
``.git/HEAD`` and one set of checked-out files per working tree. If two Claude
sessions run in the same directory, a ``git checkout`` (or reset/clean/rebase/
merge/pull/stash/restore) in one session silently moves the branch and rewrites
files under the *other* session, corrupting its in-flight edits.

This hook runs on every Bash command (no ``if`` gate) and inspects the command
string itself: ``GIT_RE`` matches a HEAD- or working-tree-mutating git
subcommand wherever it sits, including inside a compound command such as
``cd x && git checkout``. When such a subcommand is present AND 2+ ``claude``
processes have their cwd inside this repo, it returns
``permissionDecision: "deny"`` -- a hard block -- so one session can never yank
the branch / files out from under another. Otherwise it stays silent (no
decision -> normal permission flow).

Running on every Bash call costs one ``python3`` spawn per command; the regex
check exits immediately when no guarded git op is present, so the overhead is a
process start, not real work.

Detection uses ``/proc`` and is Linux/WSL-only; on platforms without ``/proc``
it degrades to silent allow and never blocks.

To relax this back to a confirmation prompt, change ``"deny"`` to ``"ask"``
below; to restore the fast path that only fires for direct ``git ...`` commands,
re-add ``"if": "Bash(git *)"`` to the hook entry in ``.claude/settings.json``
(note that gate lets compound-command git ops bypass the guard).
"""
import glob
import json
import os
import re
import sys

# Subcommands that move HEAD and/or overwrite working-tree files (shared state).
GUARDED = (
    "checkout", "switch", "reset", "clean",
    "rebase", "merge", "pull", "stash", "restore",
)
# Match `git <global-flags>* <guarded-subcommand>` so the guarded word must sit
# in the subcommand position (avoids matching e.g. `git log --grep checkout`).
GIT_RE = re.compile(
    r"\bgit\b(?:\s+(?:-c\s+\S+|-C\s+\S+|--\S+|-[A-Za-z]))*\s+(" + "|".join(GUARDED) + r")\b"
)


def repo_root(start):
    """Nearest ancestor of `start` containing a .git entry, or None."""
    d = os.path.abspath(start)
    while True:
        dotgit = os.path.join(d, ".git")
        if os.path.isdir(dotgit) or os.path.isfile(dotgit):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def claude_sessions_in(repo):
    """All `claude` PIDs whose cwd is inside `repo` (Linux/WSL only)."""
    if not os.path.isdir("/proc"):
        return []  # cannot detect -> caller degrades to allow
    repo = os.path.realpath(repo)
    found = []
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            if open(entry + "/comm").read().strip() != "claude":
                continue
            cwd = os.path.realpath(os.readlink(entry + "/cwd"))
        except OSError:
            continue  # process vanished or not ours to inspect
        if cwd == repo or cwd.startswith(repo + os.sep):
            found.append((int(os.path.basename(entry)), cwd))
    return found


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable input -> stay out of the way

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not GIT_RE.search(cmd):
        sys.exit(0)  # not a guarded git op

    repo = repo_root(data.get("cwd") or os.getcwd())
    if not repo:
        sys.exit(0)  # not in a git repo -> nothing to protect

    # >= 2 because exactly one is this very session running the command.
    sessions = claude_sessions_in(repo)
    if len(sessions) < 2:
        sys.exit(0)

    listing = "; ".join(f"pid {p}" for p, _ in sessions)
    reason = (
        f"BLOCKED: {len(sessions)} Claude sessions (incl. this one: {listing}) have "
        f"their cwd in this repo. This command mutates shared git HEAD / working-tree "
        f"state and would change the branch or files under the other session(s), "
        f"corrupting their in-flight edits. Isolate first with a git worktree (one "
        f"branch per session): `git worktree add ../<dir> <branch>` and run there. "
        f"To override for a one-off, temporarily comment out the hook in "
        f".claude/settings.json (or stop the other sessions so only one remains)."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
