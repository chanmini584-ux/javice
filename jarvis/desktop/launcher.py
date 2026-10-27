"""Opening JARVIS in its own app window, and noticing that it is already running."""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
import urllib.request
import webbrowser
from typing import Callable, Mapping


def find_app_browser(
    exists: Callable[[str], bool] = os.path.exists,
    env: Mapping[str, str] = os.environ,
    join: Callable[..., str] = ntpath.join,
) -> str | None:
    """Edge (always on Windows 10/11) or Chrome: both can show a page as a chromeless app window."""
    pf = env.get("ProgramFiles", "C:\\Program Files")
    pf86 = env.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    local = env.get("LOCALAPPDATA", "")
    candidates = [
        join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    if local:
        candidates.append(join(local, "Google", "Chrome", "Application", "chrome.exe"))
    return next((c for c in candidates if exists(c)), None)


def _spawn(args: list[str]) -> None:
    subprocess.Popen(args, close_fds=True)


def open_ui(
    url: str,
    finder: Callable[[], str | None] = find_app_browser,
    spawn: Callable[[list[str]], None] = _spawn,
    fallback: Callable[[str], object] = webbrowser.open,
) -> None:
    """Show the web app (the same PWA a phone uses) in an app window; else in the default browser."""
    browser = finder()
    if browser:
        try:
            spawn([browser, f"--app={url}", "--window-size=1100,820"])
            return
        except OSError:
            pass
    fallback(url)


def already_running(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """True when a JARVIS server already answers on this port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as r:
            return json.load(r) == {"status": "ok"}
    except (OSError, ValueError):
        return False
