"""UkoreHub Portal — the hand-off tier between UkoreHubLauncher.exe and
app/launcher.py.

Owns everything UkoreHubLauncher.exe used to do past "update and spawn the
next thing": the first-run workspace-location prompt, bootstrapping/
updating the nested app/ clone, installing app/'s Python dependencies,
checking git-lfs, and GitHub login. The exe itself now only self-updates
and bootstraps/updates Portal's own checkout before spawning this file —
see developer/launcher/launcher_build/updater.py's
_launch/_do_prelaunch_work.

Portal is its own repo (a sibling of app/, not nested inside it)
specifically so this file can own login/update logic without app/'s
closed core/core_api import-boundary rules getting in the way — see this
merged dev repo's root CLAUDE.md. portal/core/ is therefore a vendored
copy of the relevant app/core pieces, same reasoning as
developer/launcher/launcher_build/core/ — see login_dialog.py's and
git_update.py's own docstrings.

This window is purely a loading/status screen, not something a user
normally interacts with: preflight (workspace prompt/update/deps/git-lfs/
login) runs on a background QThread (_PreflightWorker) so
UkoreHubPortal.ui's status label/progress bar stay live, and the moment
everything checks out, main() auto-spawns app/launcher.py — see
_PortalWindowController.on_ready/_launch_and_close. Portal does NOT close
itself the instant that process is spawned: app/launcher.py does its own
slow work first (dependency imports, cloud sync pull, plugin discovery)
before MainWindow ever appears, and closing Portal the moment
subprocess.Popen returns would leave the artist staring at nothing for
that whole stretch. Instead _AppLaunchWaiter watches the spawned
process's stdout on its own QThread for the literal line
app/launcher.py's _reveal_window() prints right after showMaximized() —
see _APP_READY_MARKER — and only then does _on_app_ready close Portal's
window. If that process exits without ever printing the marker (crashed,
or an older app/ checkout predating this handshake), stdout just closes
and _AppLaunchWaiter's failed signal closes Portal anyway rather than
hanging forever. pushButton_launch stays hidden through the whole
preflight path; it only ever appears, relabeled, for the one case that
genuinely needs a human click — GitHub sign-in (see on_login_needed) —
since popping a device-flow dialog and a browser tab with no warning
would be startling. No project dashboard or "Back to Portal" round-trip
yet — those are later slices layered onto this same entry point.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
)

# Same "grey_dark" palette app/interface/theme.py's real THEMES dict uses
# (mirrored in developer/launcher/launcher_build/core/theme.py too) — kept
# as a few flat constants here rather than vendoring that whole file, since
# Portal only needs a handful of these rules for its own single window.
_THEME_BACKGROUND = "#1e1f22"
_THEME_SURFACE = "#2b2d31"
_THEME_SURFACE_ALT = "#232428"
_THEME_ACCENT = "#5865f2"
_THEME_TEXT_PRIMARY = "#dcddde"
_THEME_TEXT_SECONDARY = "#96989d"
_THEME_BORDER = "#3a3c41"
_THEME_HOVER = "#35373c"

PORTAL_ROOT = Path(__file__).resolve().parent
if str(PORTAL_ROOT) not in sys.path:
    sys.path.insert(0, str(PORTAL_ROOT))

from core.github.token_store import TokenStore, TokenStoreFallbackUsed  # noqa: E402
from core.store import LocalConfigStore, SystemConfigStore  # noqa: E402

REPO_ROOT = PORTAL_ROOT.parent
APP_ROOT = REPO_ROOT / "app"

APP_REMOTE_URL = "https://github.com/tonmaiart/UkoreHub.git"
APP_BRANCH = "main"

# Same file/schema/location updater.py's own ensure_workspace_dir used to
# own (repo_root-relative, never inside cache/ or the workspace folder
# itself, since both are *derived* from this setting) — moved here so the
# exe's job stays limited to self-update + bootstrap (see that file's own
# docstring). An existing install's launcher_config.json is read as-is, so
# nobody who already answered this on the old exe-owned flow gets asked
# again.
LAUNCHER_CONFIG_FILENAME = "launcher_config.json"

# Public GitHub OAuth App Client ID (Device Flow has no client secret) —
# same Portal-baked fallback updater.py used, for when
# data/system_config.json hasn't been cloud-synced/configured yet. Setting
# > Common > GitHub OAuth Client ID still wins whenever a studio admin has
# actually set one there.
DEFAULT_GITHUB_CLIENT_ID = "Ov23liCPza6KiJ7MWZbc"

_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Must match the literal app/launcher.py's _reveal_window() prints right
# after window.showMaximized() — see that file's own comment and this
# module's docstring.
_APP_READY_MARKER = "UKOREHUB_APP_WINDOW_READY"


def _is_dev_checkout() -> bool:
    # Same signal updater.py's own _is_dev_checkout uses — a developer/
    # folder next to Portal means this is the merged UkoreHubDev dev repo,
    # where app/ is a live working tree, not an independent release clone
    # that's ever safe to fetch/reset.
    return (REPO_ROOT / "developer").exists()


def _console_python() -> str:
    # Portal itself may be running under pythonw (no console, inherited
    # from whatever interpreter UkoreHubLauncher.exe picked) — pip needs a
    # real console-subsystem interpreter, or its output has nowhere to go.
    return shutil.which("python") or sys.executable


def _load_workspace_root() -> str | None:
    config_path = REPO_ROOT / LAUNCHER_CONFIG_FILENAME
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("workspace_root") or None


def _save_workspace_root(workspace_root: str) -> None:
    config_path = REPO_ROOT / LAUNCHER_CONFIG_FILENAME
    config_path.write_text(json.dumps({"workspace_root": workspace_root}, indent=2), encoding="utf-8")


def _ensure_workspace_root(parent) -> Path:
    """First run only: asks where to put the Workspace folder — it'll hold
    cache/ (login token, local settings) and storage/ (cloned repos), both
    per-machine — and remembers the answer in launcher_config.json at
    REPO_ROOT so this never asks again on this machine. Cancelling the
    picker (rather than looping/blocking forever) falls back to a
    workspace/ folder right next to the exe — still a valid, working
    choice, just not one that survives an OS reinstall the way a separate
    drive would."""
    stored = _load_workspace_root()
    if stored:
        return Path(stored)
    QMessageBox.information(
        parent,
        "UkoreHub Setup",
        "Choose a folder for your UkoreHub Workspace.\n\n"
        "This is where your cloned repos and local app data will live — "
        "pick a drive with enough free space. You won't be asked again.",
    )
    chosen = QFileDialog.getExistingDirectory(parent, "Choose UkoreHub Workspace Folder", str(Path.home()))
    workspace_dir = chosen if chosen else str(REPO_ROOT / "workspace")
    _save_workspace_root(workspace_dir)
    return Path(workspace_dir)


def _resolve_dirs(parent) -> tuple[Path, Path, Path]:
    """(cache_dir, storage_dir, data_dir) — env vars win when a caller
    (e.g. an older exe build, or manual testing) already set all three;
    otherwise derived from the workspace root (see _ensure_workspace_root),
    which is Portal's own responsibility now."""
    cache_override = os.environ.get("UKOREHUB_CACHE_DIR")
    storage_override = os.environ.get("UKOREHUB_STORAGE_DIR")
    data_override = os.environ.get("UKOREHUB_DATA_DIR")
    if cache_override and storage_override and data_override:
        return Path(cache_override), Path(storage_override), Path(data_override)
    workspace_root = _ensure_workspace_root(parent)
    return workspace_root / "cache", workspace_root / "storage", workspace_root / "data"


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


def _apply_dark_theme(app) -> None:
    # Forces the same dark theme regardless of the Windows
    # AppsUseLightTheme registry setting Qt 6.5+ otherwise auto-detects and
    # applies on its own — UkoreHubPortalLogoWhite.png is a white-on-dark
    # asset, so a machine with Windows set to light mode would otherwise
    # render it low-contrast/washed out against a light background.
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(_THEME_BACKGROUND))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(_THEME_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base, QColor(_THEME_SURFACE_ALT))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(_THEME_SURFACE))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(_THEME_SURFACE))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(_THEME_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Text, QColor(_THEME_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Button, QColor(_THEME_SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(_THEME_TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(_THEME_ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(_THEME_TEXT_SECONDARY))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(_THEME_TEXT_SECONDARY))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(_THEME_TEXT_SECONDARY))
    app.setPalette(palette)

    app.setStyleSheet(f"""
        QWidget {{
            background-color: {_THEME_BACKGROUND};
            color: {_THEME_TEXT_PRIMARY};
            font-size: 13px;
        }}
        QLabel {{
            background: transparent;
        }}
        QPushButton {{
            background-color: {_THEME_SURFACE};
            border: 1px solid {_THEME_BORDER};
            border-radius: 4px;
            padding: 6px 12px;
        }}
        QPushButton:hover {{
            background-color: {_THEME_HOVER};
        }}
        QPushButton:pressed {{
            background-color: {_THEME_ACCENT};
        }}
        QPushButton:disabled {{
            color: {_THEME_TEXT_SECONDARY};
            background-color: {_THEME_SURFACE_ALT};
        }}
        QProgressBar {{
            background-color: {_THEME_SURFACE_ALT};
            border: 1px solid {_THEME_BORDER};
            border-radius: 4px;
            text-align: center;
            color: {_THEME_TEXT_PRIMARY};
        }}
        QProgressBar::chunk {{
            background-color: {_THEME_ACCENT};
            border-radius: 3px;
        }}
    """)


def _apply_logo(window) -> None:
    # Set programmatically rather than as a pixmap path inside the .ui
    # itself — QUiLoader resolves a .ui's embedded relative pixmap path
    # against the process's current working directory at load time, not
    # the .ui file's own location, which would silently break the moment
    # Portal is spawned with a different cwd. Resolving from PORTAL_ROOT
    # here works regardless of cwd.
    logo_label = window.findChild(QLabel, "label_logo")
    if logo_label is None:
        return
    logo_path = PORTAL_ROOT / "assets" / "images" / "UkoreHubPortalLogoWhite.png"
    pixmap = QPixmap(str(logo_path))
    if pixmap.isNull():
        return
    logo_label.setText("")
    logo_label.setPixmap(
        pixmap.scaledToWidth(300, Qt.TransformationMode.SmoothTransformation)
    )


def _spawn_launcher(cache_dir: Path, storage_dir: Path, data_dir: Path) -> subprocess.Popen:
    # pythonw (no console subsystem) + CREATE_NO_WINDOW — the artist-facing
    # app should never flash a terminal. stdout is still piped (rather than
    # left to inherit/discard) so _AppLaunchWaiter can watch for
    # _APP_READY_MARKER — that works fine even under pythonw, since a pipe
    # Popen creates itself is independent of whether the child has a real
    # console subsystem.
    launcher_path = APP_ROOT / "launcher.py"
    interpreter = shutil.which("pythonw") or sys.executable
    env = os.environ.copy()
    env["UKOREHUB_CACHE_DIR"] = str(cache_dir)
    env["UKOREHUB_STORAGE_DIR"] = str(storage_dir)
    env["UKOREHUB_DATA_DIR"] = str(data_dir)
    return subprocess.Popen(
        [interpreter, str(launcher_path)],
        cwd=str(APP_ROOT),
        env=env,
        creationflags=_CREATE_NO_WINDOW,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


class _PreflightWorker(QObject):
    """Runs on a background QThread (see main()) so UkoreHubPortal.ui's
    status label/progress bar stay live instead of the window only
    appearing once everything's already done. Never touches widgets
    directly — only emits signals; main()'s slots (on the GUI thread) do
    the actual UI updates."""

    status = Signal(str)
    warning = Signal(str, str)  # title, message
    login_needed = Signal(str)  # client_id
    ready = Signal()
    failed = Signal(str, str)  # message, title

    def __init__(self, token_store, local_config_store, system_config_store):
        super().__init__()
        self._token_store = token_store
        self._local_config_store = local_config_store
        self._system_config_store = system_config_store

    def run(self) -> None:
        self.status.emit("Checking for updates...")
        try:
            _ensure_app_up_to_date()
        except Exception as exc:
            self.warning.emit(
                "Update Failed",
                f"Could not update UkoreHub:\n{exc}\n\nContinuing with the current version.",
            )

        self.status.emit("Checking for git-lfs...")
        if shutil.which("git-lfs") is None:
            self.warning.emit(
                "git-lfs Not Found",
                "'git-lfs' was not found on your PATH.\n"
                "Some repos may require it — you can continue, but LFS-tracked files "
                "may not sync correctly.",
            )

        self.status.emit("Installing required packages...")
        try:
            _ensure_app_dependencies()
        except subprocess.CalledProcessError as exc:
            self.failed.emit(
                f"Failed to install required Python packages:\n{exc}\n\n"
                "Check your internet connection and restart UkoreHub.",
                "Setup Failed",
            )
            return

        self.status.emit("Checking sign-in...")
        from core.exceptions import GitHubAuthError
        from core.github import auth as github_auth

        token = self._token_store.load_token()
        if token:
            try:
                github_auth.fetch_username(token)
            except GitHubAuthError:
                # Revoked/expired, or just offline — either way don't trust
                # a token we can't confirm; clear it and fall through to
                # login.
                self._token_store.clear_token()
                self._local_config_store.set_github_username(None)
                self._local_config_store.set_github_login_at(None)
                token = None

        if token:
            self.status.emit("Ready.")
            self.ready.emit()
            return

        client_id = self._system_config_store.github_client_id or DEFAULT_GITHUB_CLIENT_ID
        self.login_needed.emit(client_id)


class _AppLaunchWaiter(QObject):
    """Watches the just-spawned app/launcher.py process's stdout for
    _APP_READY_MARKER on a background QThread (see
    _PortalWindowController._launch_and_close) so the blocking
    line-by-line read never freezes Portal's own GUI event loop. Emits
    failed (not just silence) when the process's stdout closes without the
    marker ever showing up — a crash before MainWindow, or an older app/
    checkout that doesn't print it yet — so Portal still closes instead of
    waiting forever."""

    ready = Signal()
    failed = Signal(str)

    def __init__(self, proc: subprocess.Popen):
        super().__init__()
        self._proc = proc

    def run(self) -> None:
        if self._proc.stdout is not None:
            for line in self._proc.stdout:
                if _APP_READY_MARKER in line:
                    self.ready.emit()
                    return
        self.failed.emit("app/launcher.py exited before its window appeared")


class _PortalWindowController(QObject):
    """Owns the worker signals' slots as bound methods, not plain closures —
    a plain function has no Qt thread affinity, so Signal.connect(plain_fn)
    invokes it synchronously on whichever thread emitted the signal (the
    worker thread here) instead of queuing it onto the GUI thread. A
    QObject instance's thread affinity is the thread it was constructed on
    (main, in this case, since it's never moved) — connecting to its bound
    methods lets Qt's AutoConnection correctly detect the cross-thread case
    and queue delivery onto the GUI thread's event loop, which is what
    lets these slots safely call thread.quit()/thread.wait() without
    "QThread::wait: Thread tried to wait on itself".

    pushButton_launch has no "click to launch" role at all here — see
    module docstring. It stays hidden except in on_login_needed, where
    it's the only way forward (sign-in has to be an explicit click, not
    something that pops a browser tab on its own)."""

    def __init__(self, app, window, thread, token_store, local_config_store, cache_dir, storage_dir, data_dir):
        super().__init__()
        self._app = app
        self._window = window
        self._thread = thread
        self._token_store = token_store
        self._local_config_store = local_config_store
        self._cache_dir = cache_dir
        self._storage_dir = storage_dir
        self._data_dir = data_dir

        self.status_label = window.findChild(QLabel, "label_loading_info")
        self.progress_bar = window.findChild(QProgressBar, "progressBar_loading")
        self.launch_button = window.findChild(QPushButton, "pushButton_launch")
        self.launch_button.hide()

        # Kept alive as instance attributes (same reasoning as `controller`
        # itself in main()) for however long _AppLaunchWaiter.run() blocks
        # on readline() — created lazily in _launch_and_close, not here.
        self._wait_thread: QThread | None = None
        self._wait_worker: _AppLaunchWaiter | None = None

    def _launch_and_close(self) -> None:
        proc = _spawn_launcher(self._cache_dir, self._storage_dir, self._data_dir)
        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Starting UkoreHub...")
        self.launch_button.hide()

        self._wait_thread = QThread()
        self._wait_worker = _AppLaunchWaiter(proc)
        self._wait_worker.moveToThread(self._wait_thread)
        self._wait_thread.started.connect(self._wait_worker.run)
        self._wait_worker.ready.connect(self._on_app_launched)
        self._wait_worker.failed.connect(self._on_app_launch_failed)
        self._wait_thread.start()

    def _on_app_launched(self) -> None:
        self._wait_thread.quit()
        self._wait_thread.wait()
        self._window.close()

    def _on_app_launch_failed(self, message: str) -> None:
        # Still close rather than leave Portal sitting there forever — the
        # spawned process is on its own now either way (it may have shown
        # its own error dialog already, e.g. the Login Required gate in
        # app/launcher.py's main()).
        self._wait_thread.quit()
        self._wait_thread.wait()
        self._window.close()

    def on_status(self, text: str) -> None:
        self.status_label.setText(text)

    def on_warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self._window, title, message)

    def on_ready(self) -> None:
        self._thread.quit()
        self._thread.wait()
        self._launch_and_close()

    def on_login_needed(self, client_id: str) -> None:
        self._thread.quit()
        self._thread.wait()
        self.progress_bar.hide()
        self.status_label.setText("Sign-in required to continue.")

        def _start_login() -> None:
            from login_dialog import LoginDialog

            dialog = LoginDialog(client_id, self._window)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self._app.exit(1)
                return
            try:
                self._token_store.save_token(dialog.token)
            except TokenStoreFallbackUsed as exc:
                QMessageBox.warning(self._window, "GitHub Login", str(exc))
            self._local_config_store.set_github_username(dialog.username)
            self._local_config_store.set_github_login_at(datetime.now(timezone.utc).isoformat())
            self._launch_and_close()

        self.launch_button.setText("Sign in")
        self.launch_button.clicked.connect(_start_login)
        self.launch_button.show()

    def on_failed(self, message: str, title: str) -> None:
        self._thread.quit()
        self._thread.wait()
        QMessageBox.critical(self._window, title, message)
        self._app.exit(1)


def main() -> None:
    app = QApplication(sys.argv)
    _apply_dark_theme(app)

    loader = QUiLoader()
    ui_file = QFile(str(PORTAL_ROOT / "UkoreHubPortal.ui"))
    ui_file.open(QIODevice.OpenModeFlag.ReadOnly)
    window = loader.load(ui_file)
    ui_file.close()
    window.setWindowTitle("UkoreHub Portal")
    _apply_logo(window)

    status_label = window.findChild(QLabel, "label_loading_info")
    progress_bar = window.findChild(QProgressBar, "progressBar_loading")
    status_label.setText("Preparing...")
    progress_bar.setRange(0, 0)  # indeterminate while preflight runs
    window.show()

    cache_dir, storage_dir, data_dir = _resolve_dirs(window)
    cache_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    token_store = TokenStore(cache_dir / "github_token.json")
    local_config_store = LocalConfigStore(cache_dir / "local_config.json")
    system_config_store = SystemConfigStore(data_dir / "system_config.json")

    thread = QThread()
    worker = _PreflightWorker(token_store, local_config_store, system_config_store)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    # Kept alive via this local variable for main()'s lifetime (which lasts
    # until app.exec() returns) — Qt would otherwise garbage-collect an
    # unreferenced QObject with live signal connections.
    controller = _PortalWindowController(
        app, window, thread, token_store, local_config_store, cache_dir, storage_dir, data_dir
    )
    worker.status.connect(controller.on_status)
    worker.warning.connect(controller.on_warning)
    worker.ready.connect(controller.on_ready)
    worker.login_needed.connect(controller.on_login_needed)
    worker.failed.connect(controller.on_failed)

    thread.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
