#!/usr/bin/env python3
"""PreToolUse(Bash) hook: redirect branch switches into per-branch worktrees.

Cross-platform, shell-independent replacement for the old ``cw`` launcher (no
``~/.bashrc`` dependency). When 2+ Claude sessions share one working tree and a
Bash command tries to switch branches (``git switch`` / ``git checkout
<branch>`` / ``-b``), this hook:

  1. ensures a dedicated worktree exists at
     ``<repo>/.claude/worktrees/<branch>`` (creating it with ``git worktree
     add`` if needed), then
  2. DENIES the original command, telling the session to ``cd`` into that
     worktree and run the op there -- so the shared tree's HEAD / files are
     never moved under the other sessions.

Other working-tree-mutating git ops (reset / clean / rebase / merge / pull /
stash / restore, and file-restoring checkouts) are DENIED with advice but not
auto-worktree'd -- there is no target branch to map to a worktree.

A hook cannot ``cd`` the shell or launch a session, so it provisions + redirects
and the agent performs the ``cd``. When only this session occupies the tree (or
the command runs inside an isolated worktree), nothing is blocked. Detection
uses ``/proc`` + ``git`` and is Linux/WSL-only; elsewhere it degrades to a
silent allow.

The interesting logic lives in importable functions (``parse_git_op``,
``ensure_worktree``) so it can be unit-tested without the ``/proc`` gate.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys

# Working-tree-mutating subcommands that are NOT branch switches. These get a
# deny-with-advice (no auto-worktree -- no target branch to map).
MUTATIONS = {"reset", "clean", "rebase", "merge", "pull", "stash", "restore"}
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def _git(args, cwd=None):
    """Run git; return (returncode, stdout-stripped). Never raises."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15,
        )
    except Exception as e:  # noqa: BLE001 - degrade gracefully
        return 1, str(e)
    return r.returncode, (r.stdout or "").strip()


def _tokens(segment):
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def parse_git_op(command):
    """Classify the first guarded git op in `command`.

    Returns a dict {kind, sub, branch, is_new} or None.
      kind == 'switch'   -> a branch switch; `branch`/`is_new` populated.
      kind == 'mutation' -> a shared-tree-mutating op (incl. file checkouts).
    """
    for segment in _SEGMENT_SPLIT.split(command):
        toks = _tokens(segment)
        if "git" not in toks:
            continue
        rest = toks[toks.index("git") + 1:]
        # Skip git global flags: -C <path>, -c <kv>, --<long>, -<x>.
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in ("-C", "-c"):
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            break
        if i >= len(rest):
            continue
        sub = rest[i]
        subargs = rest[i + 1:]

        if sub == "switch":
            branch, is_new = _switch_target(subargs)
            return {"kind": "switch", "sub": sub, "branch": branch, "is_new": is_new}
        if sub == "checkout":
            branch, is_new, file_op = _checkout_target(subargs)
            if branch and not file_op:
                return {"kind": "switch", "sub": sub, "branch": branch, "is_new": is_new}
            return {"kind": "mutation", "sub": sub, "branch": None, "is_new": False}
        if sub in MUTATIONS:
            return {"kind": "mutation", "sub": sub, "branch": None, "is_new": False}
    return None


def _switch_target(subargs):
    """(branch, is_new) for `git switch ...`. switch is always branch-oriented."""
    new = None
    j = 0
    while j < len(subargs):
        a = subargs[j]
        if a in ("-c", "-C", "--create", "--force-create"):
            new = subargs[j + 1] if j + 1 < len(subargs) else None
            j += 2
            continue
        if a == "--":
            j += 1
            continue
        if a.startswith("-"):
            j += 1
            continue
        return (new or a, new is not None)
    return (new, new is not None)


def _checkout_target(subargs):
    """(branch, is_new, file_op) for `git checkout ...`.

    Distinguishes a branch switch from a file restore. `-b/-B` => new branch.
    A `--` or a non-branch first arg => file/path op (caller verifies branchness
    separately via is_branch for the bare `git checkout <name>` form).
    """
    new = None
    j = 0
    while j < len(subargs):
        a = subargs[j]
        if a in ("-b", "-B"):
            new = subargs[j + 1] if j + 1 < len(subargs) else None
            return (new, True, False)
        if a == "--":
            return (None, False, True)  # explicit file form
        if a.startswith("-"):
            j += 1
            continue
        # First positional: could be a branch or a path. Caller resolves.
        return (a, False, False)
    return (None, False, True)


def is_branch(top, name):
    """True if `name` resolves to a local or origin-tracked branch."""
    if not name:
        return False
    rc, _ = _git(["-C", top, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=top)
    if rc == 0:
        return True
    rc, _ = _git(["-C", top, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"], cwd=top)
    return rc == 0


def toplevel(path):
    rc, out = _git(["-C", path, "rev-parse", "--show-toplevel"], cwd=path)
    return os.path.realpath(out) if rc == 0 and out else None


def worktree_for_branch(top, branch):
    """Path of the worktree where `branch` is checked out, or None."""
    rc, out = _git(["-C", top, "worktree", "list", "--porcelain"], cwd=top)
    if rc != 0:
        return None
    cur = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and line[len("branch "):] == f"refs/heads/{branch}":
            return cur
    return None


def _safe(branch):
    return branch.replace("/", "-")


def ensure_worktree(top, branch, is_new):
    """Ensure a worktree for `branch` exists. Returns (path, status, error)."""
    existing = worktree_for_branch(top, branch)
    if existing:
        return existing, "exists", None
    path = os.path.join(top, ".claude", "worktrees", _safe(branch))
    if os.path.isdir(path):
        return path, "reused-dir", None
    if is_new:
        rc, out = _git(["-C", top, "worktree", "add", path, "-b", branch], cwd=top)
    else:
        rc, out = _git(["-C", top, "worktree", "add", path, branch], cwd=top)
        if rc != 0:  # branch may not exist yet -> fall back to creating it
            rc, out = _git(["-C", top, "worktree", "add", path, "-b", branch], cwd=top)
    if rc != 0:
        return None, "error", out
    return path, "created", None


def _deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _claude_sharers(top):
    """Claude PIDs whose cwd resolves to the same working tree as `top`."""
    if not os.path.isdir("/proc"):
        return []
    found = []
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            if open(entry + "/comm").read().strip() != "claude":
                continue
            cwd = os.path.realpath(os.readlink(entry + "/cwd"))
        except OSError:
            continue
        if toplevel(cwd) == top:
            found.append(int(os.path.basename(entry)))
    return found


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    op = parse_git_op(cmd)
    if not op:
        sys.exit(0)  # no guarded git op -> stay out of the way

    top = toplevel(data.get("cwd") or os.getcwd())
    if not top:
        sys.exit(0)

    sharers = _claude_sharers(top)
    if len(sharers) < 2:
        sys.exit(0)  # alone in this tree (or inside an isolated worktree) -> allow
    n = len(sharers)

    if op["kind"] == "switch":
        branch = op["branch"]
        # Resolve the ambiguous bare `git checkout <name>`: only a real branch
        # gets redirected; a file/path falls through to mutation advice.
        if op["sub"] == "checkout" and not op["is_new"] and not is_branch(top, branch):
            _deny(_mutation_reason(n, cmd))
            sys.exit(0)
        if not branch:  # e.g. `git switch -` (previous branch) -> can't map
            _deny(_mutation_reason(n, cmd))
            sys.exit(0)
        existing = worktree_for_branch(top, branch)
        if existing and os.path.realpath(existing) == os.path.realpath(top):
            sys.exit(0)  # that branch is already checked out *here* -> no-op switch
        path, status, err = ensure_worktree(top, branch, op["is_new"])
        if not path:
            _deny(
                f"Shared working tree ({n} Claude sessions). Could not auto-create a "
                f"worktree for branch '{branch}': {err}\nCreate one manually and work "
                f"there:\n  git worktree add .claude/worktrees/{_safe(branch)} "
                f"{'-b ' + branch if op['is_new'] else branch}\n  cd "
                f".claude/worktrees/{_safe(branch)}"
            )
            sys.exit(0)
        verb = {"created": "created", "exists": "already exists",
                "reused-dir": "reused"}.get(status, "ready")
        _deny(
            f"Shared working tree ({n} Claude sessions). Switching branches here "
            f"would move HEAD / rewrite files under the other sessions.\n"
            f"An isolated worktree for '{branch}' is {verb} at:\n  {path}\n"
            f"Work there instead -- run:\n  cd {path}\n"
            f"then continue (re-run your command there if still needed)."
        )
        sys.exit(0)

    # Non-switch mutation (reset/clean/rebase/merge/pull/stash/restore, or a
    # file-restoring checkout): deny with advice, no auto-worktree.
    _deny(_mutation_reason(n, cmd))
    sys.exit(0)


def _mutation_reason(n, cmd):
    return (
        f"Shared working tree ({n} Claude sessions). This command mutates shared "
        f"working-tree state and would corrupt the other sessions' in-flight "
        f"edits. Isolate your work in its own worktree first:\n"
        f"  git worktree add .claude/worktrees/<name> -b <name>\n"
        f"  cd .claude/worktrees/<name>\n"
        f"(Branch switches are auto-isolated by this hook; this op is not.)"
    )


if __name__ == "__main__":
    main()
