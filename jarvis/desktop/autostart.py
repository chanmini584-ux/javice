"""Start JARVIS when the user signs in to Windows (the per-user Run key; no admin rights needed)."""

from __future__ import annotations

import os
import sys
from typing import Callable

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JARVIS"
_AUTO = object()


def launch_command(
    frozen: bool | None = None,
    executable: str | None = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> str:
    """The command Windows runs at sign-in. --background keeps it in the tray without a window."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    if frozen:
        return f'"{exe}" --background'
    if exe.lower().endswith("python.exe"):
        windowless = exe[: -len("python.exe")] + "pythonw.exe"  # pythonw does not open a console
        if exists(windowless):
            exe = windowless
    return f'"{exe}" -m jarvis.desktop --background'


class WinRegistry:
    """The HKCU Run key. Every method raises RuntimeError anywhere but Windows."""

    @staticmethod
    def _winreg():
        if sys.platform != "win32":
            raise RuntimeError("Windows에서만 사용할 수 있습니다.")
        import winreg

        return winreg

    def get(self, name: str) -> str | None:
        w = self._winreg()
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = w.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        w = self._winreg()
        with w.CreateKey(w.HKEY_CURRENT_USER, RUN_KEY) as key:
            w.SetValueEx(key, name, 0, w.REG_SZ, value)

    def delete(self, name: str) -> None:
        w = self._winreg()
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as key:
                w.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class Autostart:
    def __init__(self, command: str | None = None, registry=_AUTO) -> None:
        self.command = command or launch_command()
        if registry is _AUTO:
            registry = WinRegistry() if sys.platform == "win32" else None
        self._reg = registry

    @property
    def available(self) -> bool:
        return self._reg is not None

    def is_enabled(self) -> bool:
        return self._reg is not None and self._reg.get(VALUE_NAME) is not None

    def enable(self) -> None:
        if self._reg is None:
            raise RuntimeError("자동 실행은 Windows에서만 설정할 수 있습니다.")
        self._reg.set(VALUE_NAME, self.command)  # also repairs an entry that points at an old path

    def disable(self) -> None:
        if self._reg is not None:
            self._reg.delete(VALUE_NAME)
