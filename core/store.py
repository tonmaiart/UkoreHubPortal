"""Trimmed vendored copy of
developer/launcher/launcher_build/core/store.py — only LocalConfigStore
and SystemConfigStore (Portal has no need for the MetadataStore/Project/
Repo domain yet, unlike the launcher repo's copy). Same on-disk schema as
app/core/storage/config_store.py's real versions, so both sides can read/
write cache_dir/local_config.json and data_dir/system_config.json safely —
see github/auth.py's module docstring for why this is vendored rather than
imported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

DEFAULT_THEME_NAME = "grey_dark"

LOCAL_CONFIG_SCHEMA_VERSION = 1
SYSTEM_CONFIG_SCHEMA_VERSION = 1


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


class LocalConfigStore:
    """Per-machine settings — never shared, gitignored. Only the fields
    Portal itself touches (github_username/github_login_at) are written
    here, but every other known field is round-tripped through
    load()/save() unchanged so this never clobbers what app/launcher.py's
    own LocalConfigStore wrote."""

    def __init__(self, json_path: Path):
        self.json_path = Path(json_path)
        self.workspace_root: str | None = None
        self.theme: str = DEFAULT_THEME_NAME
        self.active_project_id: str | None = None
        self.active_repo_id: str | None = None
        self.github_username: str | None = None
        self.github_login_at: str | None = None
        self.load()

    def load(self) -> None:
        if not self.json_path.exists():
            return
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.workspace_root = data.get("workspace_root")
        self.theme = data.get("theme", DEFAULT_THEME_NAME)
        self.active_project_id = data.get("active_project_id")
        self.active_repo_id = data.get("active_repo_id")
        self.github_username = data.get("github_username")
        self.github_login_at = data.get("github_login_at")

    def save(self) -> None:
        _atomic_write(
            self.json_path,
            {
                "schema_version": LOCAL_CONFIG_SCHEMA_VERSION,
                "workspace_root": self.workspace_root,
                "theme": self.theme,
                "active_project_id": self.active_project_id,
                "active_repo_id": self.active_repo_id,
                "github_username": self.github_username,
                "github_login_at": self.github_login_at,
            },
        )

    def set_github_username(self, username: str | None) -> None:
        self.github_username = username
        self.save()

    def set_github_login_at(self, timestamp: str | None) -> None:
        self.github_login_at = timestamp
        self.save()


class SystemConfigStore:
    """Studio-wide settings shared to everyone via Cloudflare R2 (see the
    app repo's core/vcs/cloud_sync.py). Portal only ever reads
    github_client_id from this — never writes it, never pushes to R2
    (that push only happens once app/launcher.py's own cloud_sync wiring
    runs), so this local copy is a fallback, not the source of truth."""

    def __init__(self, json_path: Path, *, on_save: Callable[[], None] | None = None):
        self.json_path = Path(json_path)
        self.github_client_id: str | None = None
        self.r2_bucket_name: str | None = None
        self.on_save = on_save
        self.load()

    def load(self) -> None:
        if not self.json_path.exists():
            return
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.github_client_id = data.get("github_client_id")
        self.r2_bucket_name = data.get("r2_bucket_name")
