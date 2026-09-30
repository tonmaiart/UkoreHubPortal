"""Portal's side of the R2 cloud sync: pulls every shared JSON blob into
data_dir before app/launcher.py ever runs, so the project picker and
plugin_update.py both work off the latest data rather than whatever the
app pulled last time.

Pull-only, and a trimmed vendored copy of app/core/vcs/cloud_sync.py's
R2JsonSync.pull (Portal can't import app/ — see main.py's module
docstring). Pushing stays the app's job: app/core/vcs/cloud_sync.py's
push() needs the ETag last seen for each blob as its If-Match
precondition, so every ETag seen here is written to ETAGS_FILENAME in
cache_dir and handed to app/launcher.py via the UKOREHUB_CLOUD_ETAGS env
var, which seeds its own R2JsonSync instead of pulling everything a
second time. Keep this file's blob list in sync with app/launcher.py's
own pull fallback.

Credentials come from the same UKOREHUB_R2_* env vars UkoreHubLauncher.exe
already sets on Portal's process (see
developer/launcher/launcher_build/updater.py) — never from a JSON file.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

ETAGS_FILENAME = "cloud_etags.json"

_FIXED_BLOBS = ("projects.json", "programs.json", "system_config.json")
_TIMEOUT_SECONDS = 20


class CloudSyncUnavailable(Exception):
    pass


def _bucket_name(data_dir: Path, appdata_dir: Path) -> str | None:
    # Same bootstrap order as app/launcher.py's _build_cloud_sync: the
    # local system_config.json is itself one of the synced blobs, so a
    # fresh machine falls back to the env var, then app/appdata's
    # git-tracked default.
    try:
        name = json.loads((data_dir / "system_config.json").read_text(encoding="utf-8")).get("r2_bucket_name")
    except (OSError, ValueError):
        name = None
    if name:
        return name
    if os.environ.get("UKOREHUB_R2_BUCKET_NAME"):
        return os.environ["UKOREHUB_R2_BUCKET_NAME"]
    try:
        return json.loads((appdata_dir / "system_config.default.json").read_text(encoding="utf-8")).get(
            "r2_bucket_name"
        )
    except (OSError, ValueError):
        return None


def _build_client():
    account_id = os.environ.get("UKOREHUB_R2_ACCOUNT_ID")
    access_key_id = os.environ.get("UKOREHUB_R2_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("UKOREHUB_R2_SECRET_ACCESS_KEY")
    if not all([account_id, access_key_id, secret_access_key]):
        return None
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(signature_version="s3v4", connect_timeout=_TIMEOUT_SECONDS, read_timeout=_TIMEOUT_SECONDS),
    )


def _read_project_ids(index_path: Path) -> list[str]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    # schema_version < 2 still has repos embedded — nothing per-project to
    # pull yet, same as app/core's read_project_ids returning None.
    if data.get("schema_version", 1) < 2:
        return []
    return [project["id"] for project in data.get("projects", []) if project.get("id")]


def pull_shared_data(
    *, data_dir: Path, appdata_dir: Path, cache_dir: Path, on_status: Callable[[str], None] | None = None
) -> Path | None:
    """Returns the ETags file path to hand to app/launcher.py, or None when
    cloud sync isn't configured on this machine (no UKOREHUB_R2_* env vars
    or no bucket name) — the app then stays local-only, same as before.
    Raises CloudSyncUnavailable on a network/R2 failure; the caller decides
    whether to carry on with the local copy."""
    from botocore.exceptions import BotoCoreError, ClientError

    status = on_status or (lambda _msg: None)
    client = _build_client()
    bucket = _bucket_name(data_dir, appdata_dir)
    if client is None or not bucket:
        return None

    etags: dict[str, str | None] = {}

    def _pull(blob_name: str) -> None:
        try:
            response = client.get_object(Bucket=bucket, Key=blob_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in ("NoSuchKey", "404"):
                etags[blob_name] = None
                return
            raise
        local_path = data_dir / blob_name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(response["Body"].read())
        etags[blob_name] = response["ETag"]

    status("Syncing shared data from cloud...")
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(_pull, _FIXED_BLOBS))
        project_ids = _read_project_ids(data_dir / "projects.json")
        if project_ids:
            status("Syncing project data from cloud...")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_pull, [f"projects/{project_id}.json" for project_id in project_ids]))
    except (BotoCoreError, ClientError, OSError) as exc:
        raise CloudSyncUnavailable(str(exc)) from exc

    etags_path = cache_dir / ETAGS_FILENAME
    etags_path.parent.mkdir(parents=True, exist_ok=True)
    etags_path.write_text(json.dumps(etags, indent=2), encoding="utf-8")
    return etags_path


def list_projects(data_dir: Path) -> list[tuple[str, str]]:
    """(id, name) for every project in data_dir's projects.json index."""
    try:
        data = json.loads((data_dir / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        (project["id"], project.get("name") or project["id"])
        for project in data.get("projects", [])
        if project.get("id")
    ]
