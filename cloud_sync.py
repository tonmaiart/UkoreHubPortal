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
second time. _FIXED_BLOBS must match app/launcher.py's _SHARED_FIXED_BLOBS
(checked by developer/app/tests/test_portal_app_contract.py).

A blob that fails to pull is left out of the ETags file rather than
failing the whole pull: the app's push() of a blob with no seeded ETag is
create-only (IfNoneMatch="*"), so it can only ever conflict and re-pull,
never clobber the cloud copy — whereas falling back to local-only for the
whole session would silently drop every edit made in it (next launch's
pull overwrites them).

Credentials come from the same UKOREHUB_R2_* env vars UkoreHubLauncher.exe
already sets on Portal's process (see
developer/launcher/launcher_build/updater.py) — never from a JSON file.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from core.store import atomic_write_bytes, read_json

ETAGS_FILENAME = "cloud_etags.json"

_FIXED_BLOBS = ("projects.json", "programs.json", "system_config.json")
_TIMEOUT_SECONDS = 20

logger = logging.getLogger("PortalCloudSync")


class CloudSyncUnavailable(Exception):
    pass


def _bucket_name(data_dir: Path, appdata_dir: Path) -> str | None:
    # Same bootstrap order as app/launcher.py's _build_cloud_sync: the
    # local system_config.json is itself one of the synced blobs, so a
    # fresh machine falls back to the env var, then app/appdata's
    # git-tracked default.
    return (
        read_json(data_dir / "system_config.json").get("r2_bucket_name")
        or os.environ.get("UKOREHUB_R2_BUCKET_NAME")
        or read_json(appdata_dir / "system_config.default.json").get("r2_bucket_name")
    )


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


def _index_projects(data_dir: Path) -> list[dict]:
    return [project for project in read_json(data_dir / "projects.json").get("projects", []) if project.get("id")]


def _read_project_ids(data_dir: Path) -> list[str]:
    # schema_version < 2 still has repos embedded — nothing per-project to
    # pull yet, same as app/core's read_project_ids returning None.
    if read_json(data_dir / "projects.json").get("schema_version", 1) < 2:
        return []
    return [project["id"] for project in _index_projects(data_dir)]


def pull_shared_data(
    *, data_dir: Path, appdata_dir: Path, cache_dir: Path, on_status: Callable[[str], None] | None = None
) -> Path | None:
    """Returns the ETags file path to hand to app/launcher.py, or None when
    cloud sync isn't configured on this machine (no UKOREHUB_R2_* env vars
    or no bucket name) — the app then stays local-only, same as before.
    Raises CloudSyncUnavailable only when R2 is unreachable altogether
    (every fixed blob failed); the caller decides whether to carry on with
    the local copy."""
    from botocore.exceptions import BotoCoreError, ClientError

    status = on_status or (lambda _msg: None)
    client = _build_client()
    bucket = _bucket_name(data_dir, appdata_dir)
    if client is None or not bucket:
        return None

    etags: dict[str, str | None] = {}
    failures: dict[str, str] = {}

    def _pull(blob_name: str) -> None:
        try:
            response = client.get_object(Bucket=bucket, Key=blob_name)
            payload = response["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in ("NoSuchKey", "404"):
                etags[blob_name] = None
                return
            failures[blob_name] = str(exc)
            return
        except (BotoCoreError, OSError) as exc:
            failures[blob_name] = str(exc)
            return
        try:
            atomic_write_bytes(data_dir / blob_name, payload)
        except OSError as exc:
            failures[blob_name] = str(exc)
            return
        etags[blob_name] = response["ETag"]

    status("Syncing shared data from cloud...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_pull, _FIXED_BLOBS))
    if all(blob_name in failures for blob_name in _FIXED_BLOBS):
        raise CloudSyncUnavailable(failures[_FIXED_BLOBS[0]])

    project_ids = _read_project_ids(data_dir)
    if project_ids:
        status("Syncing project data from cloud...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_pull, [f"projects/{project_id}.json" for project_id in project_ids]))

    for blob_name, message in failures.items():
        logger.warning("Could not pull '%s' — keeping the local copy: %s", blob_name, message)

    etags_path = cache_dir / ETAGS_FILENAME
    atomic_write_bytes(etags_path, json.dumps(etags, indent=2).encode("utf-8"))
    return etags_path


def list_projects(data_dir: Path) -> list[tuple[str, str]]:
    """(id, name) for every project in data_dir's projects.json index."""
    return [(project["id"], project.get("name") or project["id"]) for project in _index_projects(data_dir)]
