"""The one git subprocess wrapper git_update.py and plugin_update.py both
go through, so non-interactive env, no-window flags, and GitHub HTTPS auth
are set up in exactly one place.

GitHub auth mirrors app/core/vcs/git_service.py's
_github_auth_args_and_env (the signed-in token via a one-off credential
helper, never on the command line) — vendored, since Portal can't import
app/ (see main.py's module docstring).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_GITHUB_TOKEN_ENV_VAR = "UKOREHUB_GITHUB_TOKEN"
_GITHUB_HOSTS = {"github.com", "www.github.com"}


class GitError(Exception):
    pass


def _non_interactive_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh_command = env.get("GIT_SSH_COMMAND", "ssh")
    env["GIT_SSH_COMMAND"] = f"{ssh_command} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    return env


def run_git(args: list[str], cwd: Path, *, token: str | None = None, url: str = "") -> str:
    """Raises GitError on a non-zero exit. token is only ever used for an
    https github.com url — never sent to any other host."""
    auth_args: list[str] = []
    env = _non_interactive_env()
    parsed = urllib.parse.urlparse(url)
    if token and parsed.scheme == "https" and parsed.hostname in _GITHUB_HOSTS:
        helper = f'!f() {{ echo username=x-access-token; echo "password=${_GITHUB_TOKEN_ENV_VAR}"; }}; f'
        auth_args = ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
        env[_GITHUB_TOKEN_ENV_VAR] = token
    result = subprocess.run(
        [shutil.which("git") or "git", *auth_args, *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        creationflags=_NO_WINDOW_FLAGS,
    )
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_succeeds(args: list[str], cwd: Path) -> bool:
    try:
        run_git(args, cwd)
    except (GitError, OSError):
        return False
    return True
