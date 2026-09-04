"""Git bootstrap/update helpers for keeping app_root in sync with the
UkoreHub app release repo — a trimmed near-duplicate of
developer/launcher/launcher_build/updater.py's own git helpers (that file
explains why a separate repo/process vendors its own copy rather than
importing one: neither side can import the other directly). No
self-exe-relocate logic here since app_root never contains a locked,
currently-executing .exe the way the launcher's own repo_root does.

A change to one side's git-update behavior needs a matching update to the
other if it should apply to both.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Per-machine/in-flight paths `git clean -fd` must never sweep — see
# updater.py's own _CLEAN_PROTECTED_PATTERNS for the incident this
# convention fixes.
_CLEAN_PROTECTED_PATTERNS = [
    ".venv/",
    "__pycache__/",
    "*.pyc",
]


class UpdateError(Exception):
    pass


def _non_interactive_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh_command = env.get("GIT_SSH_COMMAND", "ssh")
    env["GIT_SSH_COMMAND"] = f"{ssh_command} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    return env


def _run_git(args: list[str], cwd: Path) -> str:
    git_executable = shutil.which("git") or "git"
    result = subprocess.run(
        [git_executable, *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=_non_interactive_env(),
        creationflags=_NO_WINDOW_FLAGS,
    )
    if result.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def is_git_repo(repo_root: Path) -> bool:
    return (repo_root / ".git").exists()


def bootstrap_git_repo(
    repo_root: Path, remote_url: str, branch: str, on_status: Callable[[str], None] | None = None
) -> None:
    """Turns a plain folder into a real git working tree tracking
    remote_url/branch, in place — same as updater.py's own version."""
    status = on_status or (lambda _msg: None)
    status("Setting up UkoreHub repository...")
    _run_git(["init"], cwd=repo_root)
    _run_git(["remote", "add", "origin", remote_url], cwd=repo_root)
    status("Downloading UkoreHub (first run)...")
    _run_git(["fetch", "origin", branch], cwd=repo_root)
    status("Checking out UkoreHub...")
    try:
        _run_git(["checkout", "-B", branch, "--track", f"origin/{branch}"], cwd=repo_root)
    except UpdateError:
        _run_git(["checkout", "-f", "-B", branch, "--track", f"origin/{branch}"], cwd=repo_root)


def _clean_untracked(repo_root: Path) -> None:
    args = ["clean", "-fd"]
    for pattern in _CLEAN_PROTECTED_PATTERNS:
        args.extend(["-e", pattern])
    try:
        _run_git(args, cwd=repo_root)
    except UpdateError:
        pass


def ensure_up_to_date(
    repo_root: Path, remote_url: str, branch: str, on_status: Callable[[str], None] | None = None
) -> None:
    """Forces repo_root to exactly match remote_url/branch's upstream —
    same fetch + clean -fd + reset --hard approach as updater.py's own
    ensure_up_to_date, for the same reason: nothing in app_root is worth a
    merge conflict over (per-machine files live outside it entirely, see
    root CLAUDE.md's "Program folder stays program-only")."""
    status = on_status or (lambda _msg: None)
    if not is_git_repo(repo_root):
        bootstrap_git_repo(repo_root, remote_url, branch, on_status)
        return
    status("Checking for app updates...")
    _run_git(["fetch"], cwd=repo_root)
    local_head = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    upstream_head = _run_git(["rev-parse", "@{u}"], cwd=repo_root)
    if local_head != upstream_head:
        status("Downloading app updates...")
        _clean_untracked(repo_root)
        _run_git(["reset", "--hard", upstream_head], cwd=repo_root)
    else:
        status("UkoreHub is up to date.")
