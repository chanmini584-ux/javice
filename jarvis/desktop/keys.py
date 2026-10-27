"""First-run API key entry for the windowed app (there is no console to type into)."""

from __future__ import annotations

import os
import re
from typing import Callable, MutableMapping

from ..config import Settings

SECRETS_FILE = "secrets.env"  # same file the command-line version reads at start
_KEY = re.compile(r"^[^\s=]+$")


def ensure_api_key(
    settings: Settings,
    ask_key: Callable[[], str | None],
    ask_save: Callable[[], bool],
    env: MutableMapping[str, str] = os.environ,
) -> bool:
    """True when a key is available. Asked once; written to disk only if the owner says yes."""
    if env.get("ANTHROPIC_API_KEY"):
        return True
    key = (ask_key() or "").strip()
    if not key or not _KEY.match(key):
        return False
    env["ANTHROPIC_API_KEY"] = key
    if ask_save():
        path = settings.data_dir / SECRETS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return True


def _root():
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def ask_key_dialog() -> str | None:
    try:
        from tkinter import simpledialog

        root = _root()
        try:
            return simpledialog.askstring(
                "JARVIS", "Anthropic API 키를 입력하세요.\n(console.anthropic.com 에서 발급, 입력 내용은 가려집니다)", show="*", parent=root
            )
        finally:
            root.destroy()
    except Exception:
        return None


def ask_save_dialog() -> bool:
    try:
        from tkinter import messagebox

        root = _root()
        try:
            return messagebox.askyesno(
                "JARVIS", "이 PC에 API 키를 저장할까요?\n저장하면 다음부터 묻지 않습니다. (내 사용자 폴더의 .jarvis에 저장됩니다)", parent=root
            )
        finally:
            root.destroy()
    except Exception:
        return False


def show_error(title: str, message: str) -> None:
    try:
        from tkinter import messagebox

        root = _root()
        try:
            messagebox.showerror(title, message, parent=root)
        finally:
            root.destroy()
    except Exception:
        print(f"{title}: {message}")
