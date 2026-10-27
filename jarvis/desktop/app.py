"""JARVIS desktop app entry point: server + background assistant + tray + app window."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable

from ..config import Settings


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="jarvis-desktop")
    p.add_argument("--background", action="store_true", help="start in the tray without opening a window")
    p.add_argument("--port", type=int, default=8765)
    return p.parse_args(argv)


def setup_stdio(data_dir, stdout: Any, stderr: Any) -> tuple[Any, Any]:
    """A windowed exe has no console, so stdout/stderr are None and any print would crash.
    Send them to a log file instead; real streams are left alone."""
    if stdout is not None and stderr is not None:
        return stdout, stderr
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    f = open(log_dir / "desktop.log", "a", encoding="utf-8", buffering=1)
    return (stdout if stdout is not None else f), (stderr if stderr is not None else f)


def run(
    argv: list[str],
    *,
    settings: Settings,
    handle_factory: Callable[[Settings, int], Any],
    run_tray: Callable[[Any], None],
    ensure_key: Callable[[Settings], bool],
    running: Callable[[int], bool],
    open_window: Callable[[str], None],
    show_error: Callable[[str, str], None],
    autostart: Any,
) -> int:
    from ..pc.store import PcSettingsStore
    from .tray import TrayActions

    args = parse_args(argv)
    handle = handle_factory(settings, args.port)
    if running(args.port):  # JARVIS is already up (e.g. autostart + a double-click): just show it
        if not args.background:
            open_window(handle.url)
        return 0
    if not ensure_key(settings):
        show_error("JARVIS", "Anthropic API 키가 필요합니다. 다시 실행해서 키를 입력해 주세요.")
        return 1
    try:
        handle.start()
    except RuntimeError as exc:
        show_error("JARVIS를 시작하지 못했습니다", str(exc))
        return 1
    try:
        handle.runner.start(with_voice=False, on_state=lambda kind, why: print(f"[assistant] {kind}: {why or ''}"))
        if not args.background:
            open_window(handle.url)

        def make_actions(notify, stop_tray) -> TrayActions:
            return TrayActions(
                handle, autostart, PcSettingsStore(settings.data_dir / "pc_control.json"), notify, open_window, stop_tray
            )

        try:
            run_tray(make_actions)
        except Exception as exc:  # never leave a server running with no way to quit it
            show_error("트레이 아이콘을 만들지 못했습니다", f"{type(exc).__name__}: {exc}")
            return 1
        return 0
    finally:
        handle.stop()


def exit_now(code: int) -> None:
    """End the process for certain. A tray library that failed halfway can leave a non-daemon thread
    behind, and then Python would never exit: an invisible JARVIS nobody can quit. Everything is
    already cleaned up by the time this runs."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def main(argv: list[str] | None = None) -> int:
    from .. import cli
    from . import keys
    from .autostart import Autostart
    from .launcher import already_running, open_ui
    from .server import ServerHandle
    from .tray import run_tray

    settings = Settings.from_env()
    sys.stdout, sys.stderr = setup_stdio(settings.data_dir, sys.stdout, sys.stderr)
    cli._load_secrets(settings)
    return run(
        sys.argv[1:] if argv is None else argv,
        settings=settings,
        handle_factory=lambda s, port: ServerHandle(s, port=port),
        run_tray=run_tray,
        ensure_key=lambda s: keys.ensure_api_key(s, keys.ask_key_dialog, keys.ask_save_dialog),
        running=already_running,
        open_window=open_ui,
        show_error=keys.show_error,
        autostart=Autostart(),
    )
