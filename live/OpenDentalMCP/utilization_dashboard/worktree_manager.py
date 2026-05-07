"""Git-worktree lifecycle for L2/L3 agent proposals.

Each proposal runs in its own throwaway worktree under .claude/worktrees/
proposal-<project>-<bullet>/. The agent has Read/Grep/Glob/Write/Edit access
inside that worktree, isolated from main. After the agent finishes we capture
a diff vs. main; the user (L2) or auto-apply logic (L3) decides what to do.

The worktree shares the .git of the main checkout so creation is fast and
disk-cheap.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Resolve the git root (works whether we're in the live tree or a worktree).
def _find_git_root(start: Path) -> Path:
    cur = start
    for _ in range(8):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start


GIT_ROOT = _find_git_root(Path(__file__).resolve())
WORKTREES_DIR = GIT_ROOT / ".claude" / "worktrees"
DEFAULT_BASE = "main"


@dataclass
class Worktree:
    project_id: str
    bullet_hash: str
    path: Path
    branch: str


_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(token: str) -> str:
    """Strip anything that isn't alphanumeric/dash/underscore. Keeps branch
    names valid and worktree paths shell-safe."""
    return _SAFE_TOKEN_RE.sub("-", token)[:48]


def proposal_dirname(project_id: str, bullet_hash: str) -> str:
    return f"proposal-{_sanitize(project_id)}-{_sanitize(bullet_hash)}"


def proposal_branch(project_id: str, bullet_hash: str) -> str:
    return f"proposal/{_sanitize(project_id)}/{_sanitize(bullet_hash)}"


def _git(cmd: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    # Force utf-8 decoding — git emits utf-8 but Python on Windows defaults
    # to cp1252 and chokes on diff bytes outside that codepage (e.g. fancy
    # quotes the agent wrote into a README).
    return subprocess.run(
        ["git"] + cmd,
        cwd=cwd or GIT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def create(project_id: str, bullet_hash: str, base: str = DEFAULT_BASE) -> Worktree:
    """Create a fresh worktree for one (project, bullet) pair. If a worktree
    already exists at that path, remove it first."""
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    name = proposal_dirname(project_id, bullet_hash)
    branch = proposal_branch(project_id, bullet_hash)
    wt_path = WORKTREES_DIR / name

    # Stale state cleanup. We tolerate failures from any of these — the
    # subsequent worktree-add will fail loudly if there's a real problem.
    if wt_path.exists():
        log.info("worktree dir already exists, cleaning up: %s", wt_path)
        _git(["worktree", "remove", "--force", str(wt_path)])
    _git(["worktree", "prune"])
    # Delete the branch if it exists, so worktree-add can recreate it from base
    _git(["branch", "-D", branch])

    res = _git(["worktree", "add", "-b", branch, str(wt_path), base])
    if res.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed: {res.stderr.strip()[:500]}"
        )
    log.info("worktree created at %s on branch %s", wt_path, branch)
    return Worktree(project_id=project_id, bullet_hash=bullet_hash,
                    path=wt_path, branch=branch)


def _stage_all(wt: Worktree) -> None:
    """Stage everything the agent touched, including new files. Necessary
    because untracked files don't show up in `git diff` otherwise."""
    _git(["add", "-A"], cwd=wt.path)


def diff_against_base(wt: Worktree, base: str = DEFAULT_BASE) -> str:
    """Return the unified diff of the worktree's staged state vs. base.

    The agent leaves files untracked / modified but doesn't commit. We stage
    them, then diff cached vs. base. This catches new files too.
    """
    _stage_all(wt)
    res = _git(["diff", "--cached", base, "--", "."], cwd=wt.path)
    if res.returncode != 0:
        log.warning("diff failed: %s", res.stderr.strip()[:300])
        return ""
    return res.stdout


def files_changed(wt: Worktree, base: str = DEFAULT_BASE) -> list[str]:
    _stage_all(wt)
    res = _git(["diff", "--cached", "--name-only", base], cwd=wt.path)
    if res.returncode != 0:
        return []
    return [ln for ln in res.stdout.strip().splitlines() if ln]


def apply_to_main(wt: Worktree, base: str = DEFAULT_BASE) -> tuple[bool, str]:
    """Merge the worktree's branch into base in the main checkout. Returns
    (success, message)."""
    # Make sure all the agent's changes are committed on the proposal branch
    # before we try to merge. The agent doesn't commit on its own.
    _git(["add", "-A"], cwd=wt.path)
    has_diff = _git(["diff", "--cached", "--quiet"], cwd=wt.path).returncode == 1
    if has_diff:
        commit = _git(
            ["commit", "-m",
             f"Agent proposal: {wt.project_id} / {wt.bullet_hash}",
             "--no-verify"],
            cwd=wt.path,
        )
        if commit.returncode != 0:
            return False, f"commit failed: {commit.stderr.strip()[:300]}"

    # Merge the proposal branch into base from the main checkout.
    res = _git(["merge", "--no-ff", "-m",
                f"Merge proposal {wt.branch}", wt.branch])
    if res.returncode != 0:
        return False, f"merge failed: {res.stderr.strip()[:500]}"
    return True, f"merged {wt.branch} into {base}"


def discard(wt: Worktree) -> tuple[bool, str]:
    """Remove the worktree and delete its branch."""
    res = _git(["worktree", "remove", "--force", str(wt.path)])
    if res.returncode != 0:
        return False, f"worktree remove failed: {res.stderr.strip()[:300]}"
    _git(["branch", "-D", wt.branch])
    return True, f"worktree {wt.path.name} discarded"
