"""GitHub device-flow login dialog — a PySide6 port of
UkoreHubLauncher's tkinter _LoginDialog (developer/launcher/launcher_build/
updater.py). That version used tkinter specifically to avoid pulling
~45MB of Qt into the compiled exe (see its own module docstring) — Portal
already depends on PySide6 for its own window, so there's no reason to add
a second GUI toolkit just for this screen.

Device-flow polling runs on a QThread; Qt's cross-thread signal/slot
connection is what makes it safe to update widgets from the polling
result without a manual queue/poll-timer the way the tkinter version
needed.
"""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout

from core.exceptions import GitHubAuthError
from core.github import auth as github_auth


class _DeviceFlowWorker(QObject):
    code_ready = Signal(str, str)
    succeeded = Signal(str, str)  # username, token
    failed = Signal(str)

    def __init__(self, client_id: str | None):
        super().__init__()
        self._client_id = client_id

    def run(self) -> None:
        try:
            if not self._client_id:
                raise GitHubAuthError(
                    "GitHub Client ID not configured (data/system_config.json) — ask a studio admin."
                )
            device = github_auth.request_device_code(self._client_id)
            self.code_ready.emit(device.user_code, device.verification_uri)
            token = github_auth.poll_for_token(
                self._client_id, device.device_code, device.interval, device.expires_in
            )
            username = github_auth.fetch_username(token)
            self.succeeded.emit(username, token)
        except GitHubAuthError as exc:
            self.failed.emit(str(exc))


class LoginDialog(QDialog):
    def __init__(self, client_id: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GitHub Login")
        self.setFixedSize(400, 220)
        self.username: str | None = None
        self.token: str | None = None

        self._instructions = QLabel("Starting GitHub login...")
        self._instructions.setWordWrap(True)

        self._code_label = QLabel("")
        self._code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_font = self._code_label.font()
        code_font.setPointSize(20)
        code_font.setBold(True)
        self._code_label.setFont(code_font)

        self._copy_button = QPushButton("Copy Code")
        self._copy_button.setEnabled(False)
        self._copy_button.clicked.connect(self._copy_code)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._instructions)
        layout.addWidget(self._code_label)
        layout.addWidget(self._copy_button)
        layout.addWidget(self._progress)
        layout.addStretch(1)
        layout.addWidget(cancel_button)

        self._thread = QThread(self)
        self._worker = _DeviceFlowWorker(client_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.code_ready.connect(self._on_code_ready)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_code_ready(self, code: str, verification_uri: str) -> None:
        self._instructions.setText(
            f"Your browser should have opened to {verification_uri} — enter this code there:"
        )
        self._code_label.setText(code)
        self._copy_button.setEnabled(True)
        webbrowser.open(verification_uri)

    def _on_succeeded(self, username: str, token: str) -> None:
        self.username = username
        self.token = token
        self._stop_worker()
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._instructions.setText(f"Login failed: {message}")
        self._stop_worker()

    def _copy_code(self) -> None:
        QApplication.clipboard().setText(self._code_label.text())

    def _stop_worker(self) -> None:
        self._thread.quit()
        self._thread.wait()

    def reject(self) -> None:
        self._stop_worker()
        super().reject()
