#!/usr/bin/env python3
"""SessionStart advisor: recommend worktree isolation when sessions share a tree.

Cross-session git collisions come from two Claude sessions sharing one working
tree -- one ``.git/HEAD`` and one set of checked-out files. A ``checkout`` /
``reset`` / ``rebase`` in one session then silently moves the branch and
rewrites files under the other. The structural fix is to never share a tree:
give each session its own ``git worktree``.

This hook fires once at session start. If another ``claude`` process already has
its cwd in the SAME working tree as this session, it injects a recommendation to
isolate via a worktree. Sessions living in sibling worktrees do NOT collide
(each worktree has its own HEAD) and are intentionally ignored.

It is advisory only -- SessionStart hooks cannot block a session -- which suits
the model: prevention is the worktree habit (see the ``cw`` launcher); this is
the reminder for when two sessions land in one tree anyway.

Detection uses ``/proc`` + ``git rev-parse`` and is Linux/WSL-only; elsewhere it
degrades to a silent no-op.
"""
import glob
import json
import os
import subprocess
import sys


def toplevel(path):
    """Working-tree root for `path` (worktree-aware), or None."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return os.path.realpath(top) if top else None


def claude_sessions():
    """All `claude` PIDs and their cwd (Linux/WSL only)."""
    if not os.path.isdir("/proc"):
        return []
    found = []
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            if open(entry + "/comm").read().strip() != "claude":
                continue
            cwd = os.path.realpath(os.readlink(entry + "/cwd"))
        except OSError:
            continue  # process vanished or not ours to inspect
        found.append((int(os.path.basename(entry)), cwd))
    return found


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    mine = toplevel(cwd)
    if not mine:
        sys.exit(0)  # not in a git working tree -> nothing to advise

    # Sessions whose cwd resolves to the *same* working tree. Sibling worktrees
    # are excluded automatically (each has a distinct toplevel). This session is
    # included in the count, hence the >= 2 threshold below.
    sharers = [(pid, c) for pid, c in claude_sessions() if toplevel(c) == mine]
    if len(sharers) < 2:
        sys.exit(0)

    pids = ", ".join(f"pid {p}" for p, _ in sharers)
    msg = (
        f"⚠️ Worktree-isolation advisory: {len(sharers)} Claude sessions "
        f"(incl. this one: {pids}) share this working tree:\n  {mine}\n"
        f"They share one git HEAD and one set of files, so a checkout / reset / "
        f"rebase / pull / merge / stash / restore in any one of them will move "
        f"the branch and rewrite files under the others, corrupting in-flight "
        f"edits.\n"
        f"Before any branch- or working-tree-mutating git work here, isolate this "
        f"session in its own worktree, e.g.:\n"
        f"  git worktree add ../<dir> -b <branch>   # then work there\n"
        f"or use the `cw <branch>` shell launcher, or Claude Code's EnterWorktree. "
        f"Read-only and additive ops (status, add, commit, push) are safe to share."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
