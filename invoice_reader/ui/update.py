"""Self-update support: pull the latest code from the Git repository and rebuild
the offline single-EXE installer, with live command output. Only available when
the app runs from a source checkout (not from the installed, frozen copy)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

_CREATE_NO_WINDOW = 0x08000000  # keep helper git calls from flashing a console


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def git_exe() -> str | None:
    return shutil.which("git")


def is_source_checkout() -> bool:
    root = project_root()
    return (not getattr(sys, "frozen", False)
            and (root / ".git").is_dir()
            and (root / "main.py").is_file())


def run_git(*args: str) -> tuple[int, str]:
    """Run a quick git query against the project repo; returns (exit code, output)."""
    exe = git_exe()
    if exe is None:
        return 1, "Git is not installed."
    try:
        p = subprocess.run([exe, "-C", str(project_root()), *args],
                           capture_output=True, text=True, timeout=20,
                           creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def current_branch() -> str:
    code, out = run_git("rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 else "main"


def remote_url() -> str:
    code, out = run_git("remote", "get-url", "origin")
    return out if code == 0 else ""


def set_remote(url: str) -> tuple[int, str]:
    if remote_url():
        return run_git("remote", "set-url", "origin", url)
    return run_git("remote", "add", "origin", url)


def git_summary() -> str:
    code, head = run_git("log", "-1", "--format=%h %ad", "--date=format:%b %d, %Y")
    if code != 0:
        return head or "Not a Git repository"
    origin = remote_url()
    return f"{current_branch()} @ {head}" + (f"  ({origin})" if origin else "  (no remote configured)")


class CommandDialog(QDialog):
    """Runs commands one after another, streaming their output live.

    steps: list of (label, program, args). Stops at the first failing step.
    """

    def __init__(self, title: str, steps: list[tuple[str, str, list[str]]],
                 cwd: str | Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)
        self._steps = list(steps)
        self._cwd = str(cwd)
        self._index = -1
        self.succeeded = False

        lay = QVBoxLayout(self)
        self.status = QLabel("Starting...")
        lay.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.output, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.button = QPushButton("Cancel")
        self.button.clicked.connect(self._cancel_or_close)
        row.addWidget(self.button)
        lay.addLayout(row)

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(self._cwd)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._step_finished)
        self.proc.errorOccurred.connect(self._proc_error)
        self._next_step()

    # ---- process plumbing -------------------------------------------------
    def _append(self, text: str) -> None:
        self.output.appendPlainText(text.rstrip("\n"))
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _read(self) -> None:
        self._append(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace"))

    def _next_step(self) -> None:
        self._index += 1
        label, program, args = self._steps[self._index]
        self.status.setText(f"Step {self._index + 1} of {len(self._steps)}: {label}")
        self._append(f"\n=== {label} ===\n> {program} {' '.join(args)}")
        self.proc.start(program, args)

    def _proc_error(self, _error) -> None:
        self._finish(False, f"Could not run: {self.proc.program()} ({self.proc.errorString()})")

    def _step_finished(self, exit_code: int, _status) -> None:
        self._read()
        if exit_code != 0:
            self._finish(False, f"Failed with exit code {exit_code}.")
        elif self._index + 1 < len(self._steps):
            self._next_step()
        else:
            self._finish(True, "Finished successfully.")

    def _finish(self, ok: bool, message: str) -> None:
        self.succeeded = ok
        self.status.setText(message)
        self._append(f"\n{message}")
        self.button.setText("Close")

    def _cancel_or_close(self) -> None:
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
            self._finish(False, "Cancelled.")
            return
        self.accept() if self.succeeded else self.reject()

    def closeEvent(self, event) -> None:
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
        super().closeEvent(event)
