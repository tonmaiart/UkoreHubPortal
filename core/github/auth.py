"""Vendored copy of developer/launcher/launcher_build/core/github/auth.py —
see that file's own repo context. Portal is a separate repo from both the
launcher and the app, so it keeps its own copy rather than importing
either directly; a change to the real device-flow logic needs a matching
update here too if it should also apply to Portal's own login step.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from core.exceptions import GitHubAuthError

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_API_URL = "https://api.github.com/user"


@dataclass
class DeviceCodeResponse:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def _post_json(url: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "User-Agent": "UkoreHub"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise GitHubAuthError(f"Network error contacting GitHub: {exc}") from exc


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "UkoreHub",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise GitHubAuthError(f"Network error contacting GitHub: {exc}") from exc


def request_device_code(client_id: str, scope: str = "read:user repo") -> DeviceCodeResponse:
    if not client_id:
        raise GitHubAuthError(
            "GitHub Client ID not configured — set it in Setting > Common."
        )
    data = _post_json(DEVICE_CODE_URL, {"client_id": client_id, "scope": scope})
    if "device_code" not in data:
        raise GitHubAuthError(f"Unexpected response requesting device code: {data}")
    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        expires_in=data["expires_in"],
        interval=data["interval"],
    )


def poll_for_token(
    client_id: str,
    device_code: str,
    interval: int,
    expires_in: int,
    on_tick: Callable[[], None] | None = None,
) -> str:
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        if on_tick:
            on_tick()
        data = _post_json(
            TOKEN_URL,
            {
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if "access_token" in data:
            return data["access_token"]
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = data.get("interval", interval + 5)
            continue
        if error in ("expired_token", "access_denied"):
            raise GitHubAuthError(f"GitHub login failed: {error}")
        raise GitHubAuthError(f"Unexpected response polling for token: {data}")
    raise GitHubAuthError("GitHub login timed out.")


def fetch_username(access_token: str) -> str:
    data = _get_json(USER_API_URL, access_token)
    if "login" not in data:
        raise GitHubAuthError(f"Unexpected response fetching user: {data}")
    return data["login"]
