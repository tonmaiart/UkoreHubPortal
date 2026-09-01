"""Vendored copy of
developer/launcher/launcher_build/core/github/token_store.py — same
keyring service/key names, so a token this writes is read correctly by
app/core/auth's SecureTokenStore (core.github_tokens) once app/launcher.py
runs, and vice versa. See auth.py's module docstring for why this is
vendored rather than imported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE_NAME = "UkoreHub"
KEYRING_USERNAME_KEY = "github_access_token"


class TokenStoreFallbackUsed(Exception):
    """Raised (after the token is safely saved) to tell the UI to warn the user."""


class TokenStore:
    def __init__(self, fallback_path: Path):
        self.fallback_path = Path(fallback_path)

    def save_token(self, token: str) -> None:
        try:
            import keyring

            keyring.set_password(SERVICE_NAME, KEYRING_USERNAME_KEY, token)
            if self.fallback_path.exists():
                self.fallback_path.unlink()
            return
        except Exception:
            self._save_fallback(token)
            raise TokenStoreFallbackUsed(
                "Secure system keyring unavailable — storing your GitHub token in a "
                f"local file at {self.fallback_path}. Do not share this file."
            )

    def _save_fallback(self, token: str) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.fallback_path.with_suffix(self.fallback_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"token": token}), encoding="utf-8")
        os.replace(tmp_path, self.fallback_path)

    def load_token(self) -> str | None:
        try:
            import keyring

            token = keyring.get_password(SERVICE_NAME, KEYRING_USERNAME_KEY)
            if token:
                return token
        except Exception:
            pass
        if self.fallback_path.exists():
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            return data.get("token")
        return None

    def clear_token(self) -> None:
        try:
            import keyring

            keyring.delete_password(SERVICE_NAME, KEYRING_USERNAME_KEY)
        except Exception:
            pass
        if self.fallback_path.exists():
            self.fallback_path.unlink()
