"""Trimmed vendored copy of
developer/launcher/launcher_build/core/store.py — only the LocalConfigStore/
SystemConfigStore fields Portal itself reads or writes. Same on-disk schema
as app/core/storage/config_store.py's real versions, so both sides can
read/write cache_dir/local_config.json and data_dir/system_config.json
safely — see github/auth.py's module docstring for why this is vendored
rather than imported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LOCAL_CONFIG_SCHEMA_VERSION = 1


def read_json(path: Path) -> dict:
    """{} for a missing, unreadable, or corrupt file — a half-written JSON
    file must never stop Portal from starting, since Portal's own cloud
    pull is what would replace it with a good copy."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, path)


def _atomic_write(path: Path, data: dict) -> None:
    atomic_write_bytes(path, json.dumps(data, indent=2).encode("utf-8"))


class LocalConfigStore:
    """Per-machine settings — never shared, gitignored. save() re-reads the
    file and only overwrites the keys Portal owns, so every field
    app/launcher.py's own LocalConfigStore writes (including ones added
    after this copy was vendored) survives a Portal save untouched."""

    def __init__(self, json_path: Path):
        self.json_path = Path(json_path)
        self.active_project_id: str | None = None
        self.active_repo_id: str | None = None
        self.github_username: str | None = None
        self.github_login_at: str | None = None
        self.load()

    def load(self) -> None:
        data = read_json(self.json_path)
        self.active_project_id = data.get("active_project_id")
        self.active_repo_id = data.get("active_repo_id")
        self.github_username = data.get("github_username")
        self.github_login_at = data.get("github_login_at")

    def save(self) -> None:
        data = read_json(self.json_path)
        data.setdefault("schema_version", LOCAL_CONFIG_SCHEMA_VERSION)
        data.update(
            {
                "active_project_id": self.active_project_id,
                "active_repo_id": self.active_repo_id,
                "github_username": self.github_username,
                "github_login_at": self.github_login_at,
            }
        )
        _atomic_write(self.json_path, data)

    def set_active_project(self, project_id: str | None) -> None:
        # Clears active_repo_id too, same as app/core's own version — a repo
        # id left over from another project would resolve against the wrong
        # registry.
        self.active_project_id = project_id
        self.active_repo_id = None
        self.save()

    def set_github_session(self, username: str | None, login_at: str | None) -> None:
        self.github_username = username
        self.github_login_at = login_at
        self.save()

    def set_github_login_at(self, timestamp: str | None) -> None:
        self.github_login_at = timestamp
        self.save()


class SystemConfigStore:
    """Studio-wide settings shared to everyone via Cloudflare R2 (see the
    app repo's core/vcs/cloud_sync.py). Read-only here — Portal only needs
    github_client_id, and never pushes to R2 (that's the app's job). Call
    load() again after cloud_sync.pull_shared_data() to pick up the fresh
    copy."""

    def __init__(self, json_path: Path):
        self.json_path = Path(json_path)
        self.github_client_id: str | None = None
        self.load()

    def load(self) -> None:
        self.github_client_id = read_json(self.json_path).get("github_client_id")
