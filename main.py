"""UkoreHub Portal — the hand-off tier between UkoreHubLauncher.exe and
app/launcher.py.

Owns everything UkoreHubLauncher.exe used to do past "update and spawn the
next thing": bootstrapping/updating the nested app/ clone, installing
app/'s Python dependencies, checking git-lfs, and GitHub login. The exe
itself now only self-updates and bootstraps/updates Portal's own checkout
before spawning this file — see
developer/launcher/launcher_build/updater.py's _launch/_do_prelaunch_work.

Portal is its own repo (a sibling of app/, not nested inside it)
specifically so this file can own login/update logic without app/'s
closed core/core_api import-boundary rules getting in the way — see this
merged dev repo's root CLAUDE.md. portal/core/ is therefore a vendored
copy of the relevant app/core pieces, same reasoning as
developer/launcher/launcher_build/core/ — see login_dialog.py's and
git_update.py's own docstrings.

Still just a pass-through beyond prereqs/update/login: no project
dashboard or "Back to Portal" round-trip yet (UkoreHubPortal.ui is a
single Welcome/Launch screen) — those are later slices layered onto this
same entry point.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PORTAL_ROOT = Path(__file__).resolve().parent
if str(PORTAL_ROOT) not in sys.path:
    sys.path.insert(0, str(PORTAL_ROOT))

REPO_ROOT = PORTAL_ROOT.parent
APP_ROOT = REPO_ROOT / "app"

APP_REMOTE_URL = "https://github.com/tonmaiart/UkoreHub.git"
APP_BRANCH = "main"

# Same fallback USER_DATA_DIR convention as app/launcher.py's own
# CACHE_DIR/DATA_DIR — only actually used if this file is run directly,
# bypassing UkoreHubLauncher.exe (which otherwise always sets these env
# vars before spawning Portal, see updater.py's _launch).
USER_DATA_DIR = Path.home() / "Documents" / "UkoreHub"

# Public GitHub OAuth App Client ID (Device Flow has no client secret) —
# same Portal-baked fallback updater.py used, for when
# data/system_config.json hasn't been cloud-synced/configured yet. Setting
# > Common > GitHub OAuth Client ID still wins whenever a studio admin has
# actually set one there.
DEFAULT_GITHUB_CLIENT_ID = "Ov23liCPza6KiJ7MWZbc"

_CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0


def _is_dev_checkout() -> bool:
    # Same signal updater.py's own _is_dev_checkout uses — a developer/
    # folder next to Portal means this is the merged UkoreHubDev dev repo,
    # where app/ is a live working tree, not an independent release clone
    # that's ever safe to fetch/reset.
    return (REPO_ROOT / "developer").exists()


def _console_python() -> str:
    # Portal itself may be running under pythonw (no console, inherited
    # from whatever interpreter UkoreHubLauncher.exe picked) — pip and
    # launcher.py both need a real console-subsystem interpreter, or their
    # output has nowhere to go.
    return shutil.which("python") or sys.executable


def _cache_dir() -> Path:
    return Path(os.environ.get("UKOREHUB_CACHE_DIR") or USER_DATA_DIR / "cache")


def _data_dir() -> Path:
    return Path(os.environ.get("UKOREHUB_DATA_DIR") or USER_DATA_DIR / "data")


def _ensure_app_up_to_date() -> None:
    if _is_dev_checkout():
        return
    from git_update import ensure_up_to_date

    APP_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_up_to_date(APP_ROOT, APP_REMOTE_URL, APP_BRANCH)


def _ensure_app_dependencies() -> None:
    requirements_path = APP_ROOT / "requirements.txt"
    if not requirements_path.exists():
        return
    subprocess.run(
        [
            _console_python(),
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements_path),
            "--disable-pip-version-check",
            "--quiet",
        ],
        check=True,
    )


def _spawn_launcher() -> None:
    launcher_path = APP_ROOT / "launcher.py"
    subprocess.Popen(
        [_console_python(), str(launcher_path)],
        cwd=str(APP_ROOT),
        env=os.environ.copy(),
        creationflags=_CREATE_NEW_CONSOLE,
    )


def main() -> None:
    from PySide6.QtCore import QFile, QIODevice
    from PySide6.QtUiTools import QUiLoader
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

    app = QApplication(sys.argv)

    try:
        _ensure_app_up_to_date()
    except Exception as exc:
        QMessageBox.warning(
            None,
            "Update Failed",
            f"Could not update UkoreHub:\n{exc}\n\nContinuing with the current version.",
        )

    if shutil.which("git-lfs") is None:
        QMessageBox.warning(
            None,
            "git-lfs Not Found",
            "'git-lfs' was not found on your PATH.\n"
            "Some repos may require it — you can continue, but LFS-tracked files "
            "may not sync correctly.",
        )

    try:
        _ensure_app_dependencies()
    except subprocess.CalledProcessError as exc:
        QMessageBox.critical(
            None,
            "Setup Failed",
            f"Failed to install required Python packages:\n{exc}\n\n"
            "Check your internet connection and restart UkoreHub.",
        )
        sys.exit(1)

    from core.github import auth as github_auth
    from core.github.token_store import TokenStore, TokenStoreFallbackUsed
    from core.exceptions import GitHubAuthError
    from core.store import LocalConfigStore, SystemConfigStore

    cache_dir = _cache_dir()
    data_dir = _data_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    token_store = TokenStore(cache_dir / "github_token.json")
    local_config_store = LocalConfigStore(cache_dir / "local_config.json")
    system_config_store = SystemConfigStore(data_dir / "system_config.json")

    token = token_store.load_token()
    if token:
        try:
            github_auth.fetch_username(token)
        except GitHubAuthError:
            # Revoked/expired, or just offline — either way don't trust a
            # token we can't confirm; clear it and fall through to login.
            token_store.clear_token()
            local_config_store.set_github_username(None)
            local_config_store.set_github_login_at(None)
            token = None

    if not token:
        client_id = system_config_store.github_client_id or DEFAULT_GITHUB_CLIENT_ID
        from login_dialog import LoginDialog

        dialog = LoginDialog(client_id)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(1)
        try:
            token_store.save_token(dialog.token)
        except TokenStoreFallbackUsed as exc:
            QMessageBox.warning(None, "GitHub Login", str(exc))
        local_config_store.set_github_username(dialog.username)
        local_config_store.set_github_login_at(datetime.now(timezone.utc).isoformat())

    loader = QUiLoader()
    ui_file = QFile(str(PORTAL_ROOT / "UkoreHubPortal.ui"))
    ui_file.open(QIODevice.OpenModeFlag.ReadOnly)
    window = loader.load(ui_file)
    ui_file.close()
    window.setWindowTitle("UkoreHub Portal")

    def _on_launch_clicked() -> None:
        _spawn_launcher()
        window.close()

    launch_button = window.findChild(QPushButton, "pushButton")
    launch_button.clicked.connect(_on_launch_clicked)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
