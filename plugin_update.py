"""Brings cache/plugins/ repo plugins up to date before app/launcher.py
ever runs, so a freshly-pulled plugin is simply loaded on this launch —
replaces app/plugins/core/project_editor's old in-app auto-sync, which
could only offer a "Force update and restart UkoreHub" popup after the
fact, since app/launcher.py's discover_plugins()/apply_plugins() are
one-shot at startup.

Two passes, all folders in parallel (each is its own separate clone, so
no two tasks ever touch the same folder):
- every existing clone under plugins_root: fetch, then `merge --ff-only`
  if behind. Never resets or discards anything — a clone with local
  commits or conflicting local edits (a dev working directly in
  cache/plugins/, see Settings > Account > Plugins' Bulk Push) just fails
  the fast-forward and is left exactly as it was, logged, for someone to
  sort out from that Settings tab.
- clone-if-missing for every External Plugins catalog entry the active
  project's locally-cloned repos (plus the active repo) require.

The catalog/requirements are read from data_dir's projects/<id>.json,
which cloud_sync.py has just pulled, for the project the user just
picked (Portal's project combobox, main.py's _on_cloud_synced) — both happen right before this runs.

GitHub HTTPS auth mirrors app/core/vcs/git_service.py's
_github_auth_args_and_env (the signed-in token via a one-off credential
helper, never on the command line) — vendored, since Portal can't import
app/ (see main.py's module docstring).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from git_update import _non_interactive_env

_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_GITHUB_TOKEN_ENV_VAR = "UKOREHUB_GITHUB_TOKEN"
_GITHUB_HOSTS = {"github.com", "www.github.com"}

# Must match app/plugins/core/project_editor/plugin.py's
# _EXTERNAL_PLUGINS_PLUGIN_ID — the Project.plugin_data key the catalog
# lives under.
_EXTERNAL_PLUGINS_KEY = "external_plugins"

_MAX_PARALLEL = 8

logger = logging.getLogger("PortalPlugins")


class _GitError(Exception):
    pass


@dataclass
class _CatalogEntry:
    name: str
    git_url: str
    folder_name: str


def _git(args: list[str], cwd: Path, *, token: str | None = None, url: str = "") -> str:
    auth_args: list[str] = []
    extra_env: dict = {}
    parsed = urllib.parse.urlparse(url)
    if token and parsed.scheme == "https" and parsed.hostname in _GITHUB_HOSTS:
        helper = f'!f() {{ echo username=x-access-token; echo "password=${_GITHUB_TOKEN_ENV_VAR}"; }}; f'
        auth_args = ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
        extra_env = {_GITHUB_TOKEN_ENV_VAR: token}
    env = _non_interactive_env()
    env.update(extra_env)
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
        raise _GitError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def _is_repo_root(path: Path) -> bool:
    # Same guard as GitService.is_repo_root: a broken/empty .git would let
    # git's discovery walk up to whatever real repo sits above cache/.
    if not (path / ".git").exists():
        return False
    try:
        toplevel = _git(["rev-parse", "--show-toplevel"], path)
        return Path(toplevel).resolve() == path.resolve()
    except (_GitError, OSError):
        return False


def _is_safe_folder_name(folder_name: str) -> bool:
    return bool(folder_name) and folder_name not in (".", "..") and "/" not in folder_name and "\\" not in folder_name


def _missing_required_entries(
    data_dir: Path, storage_dir: Path, plugins_root: Path, project_id: str | None, active_repo_id: str | None
) -> list[_CatalogEntry]:
    if not project_id:
        return []
    project_path = data_dir / "projects" / f"{project_id}.json"
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    required_ids: set[str] = set()
    for repo in project.get("repos", []):
        local_path = repo.get("local_path")
        is_local = bool(local_path) and (storage_dir / local_path / ".git").exists()
        if is_local or repo.get("id") == active_repo_id:
            required_ids.update(repo.get("required_plugin_ids", []))

    catalog = (project.get("plugin_data") or {}).get(_EXTERNAL_PLUGINS_KEY, {}).get("catalog", [])
    missing: list[_CatalogEntry] = []
    for raw in catalog:
        # plugin_id stays None until the app has seen this entry cloned
        # once — without it there's no way to tell whether it's required.
        if raw.get("plugin_id") not in required_ids:
            continue
        folder_name = raw.get("folder_name", "")
        if not _is_safe_folder_name(folder_name) or not raw.get("git_url"):
            continue
        if (plugins_root / folder_name / ".git").exists():
            continue
        missing.append(_CatalogEntry(raw.get("name") or folder_name, raw["git_url"], folder_name))
    return missing


def _update_clone(path: Path, token: str | None) -> str:
    if not _is_repo_root(path):
        return "skipped (broken .git)"
    if (path / ".git" / "MERGE_HEAD").exists():
        return "skipped (unresolved merge)"
    try:
        url = _git(["remote", "get-url", "origin"], path)
    except _GitError:
        return "skipped (no origin remote)"
    _git(["fetch", "origin"], path, token=token, url=url)
    try:
        counts = _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], path)
    except _GitError:
        return "skipped (no upstream branch)"
    ahead, behind = (int(value) for value in counts.split())
    if behind == 0:
        return "up to date"
    if ahead > 0:
        return f"skipped ({ahead} local commit(s) not pushed, {behind} behind)"
    try:
        _git(["merge", "--ff-only", "@{u}"], path, token=token, url=url)
    except _GitError as exc:
        return f"skipped (local changes block update: {exc})"
    return f"updated ({behind} commit(s))"


def _clone(entry: _CatalogEntry, plugins_root: Path, token: str | None) -> str:
    _git(["clone", entry.git_url, str(plugins_root / entry.folder_name)], plugins_root, token=token, url=entry.git_url)
    return "cloned"


def update_plugins(
    *,
    cache_dir: Path,
    data_dir: Path,
    storage_dir: Path,
    project_id: str | None,
    active_repo_id: str | None,
    token: str | None,
    on_status: Callable[[str], None] | None = None,
) -> None:
    status = on_status or (lambda _msg: None)
    plugins_root = cache_dir / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)

    tasks: dict[str, Callable[[], str]] = {}
    for path in sorted(plugins_root.iterdir()):
        if path.is_dir() and (path / ".git").exists():
            tasks[path.name] = lambda path=path: _update_clone(path, token)
    for entry in _missing_required_entries(data_dir, storage_dir, plugins_root, project_id, active_repo_id):
        tasks[entry.folder_name] = lambda entry=entry: _clone(entry, plugins_root, token)

    if not tasks:
        return

    total = len(tasks)
    done = 0
    status(f"Updating plugins (0/{total})...")
    with ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
        futures = {pool.submit(task): name for name, task in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            done += 1
            try:
                logger.info("%s: %s", name, future.result())
            except Exception as exc:
                logger.warning("%s: failed — %s", name, exc)
            status(f"Updating plugins ({done}/{total})...")
