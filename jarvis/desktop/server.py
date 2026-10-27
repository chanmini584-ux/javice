"""The JARVIS server and background assistant, run inside the desktop app.

The web server answers the app window and phones. The background assistant (the daemon) does
the morning briefing, daily collection and memo upkeep; voice is optional on top of it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..config import Settings

StateFn = Callable[[str, "str | None"], None]
_DEFAULT = object()


class DaemonRunner:
    """Runs the daemon on its own thread and can restart it with or without voice."""

    def __init__(self, factory: Callable[[bool], Any] | None) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._daemon: Any = None
        self._voice = False

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def voice_active(self) -> bool:
        return self._daemon is not None and self._voice

    def start(self, with_voice: bool = False, on_state: StateFn | None = None) -> None:
        """(Re)start without blocking. on_state gets ("ready"|"failed"|"stopped", reason)."""
        if self._factory is None:
            raise RuntimeError("no assistant configured")
        self.stop()
        with self._lock:
            self._cancel = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(with_voice, on_state, self._cancel), name="jarvis-daemon", daemon=True
            )
            self._thread.start()

    def _run(self, with_voice: bool, on_state: StateFn | None, cancel: threading.Event) -> None:
        # The daemon is built here, on its own thread: it owns SQLite connections.
        for wv in ([True, False] if with_voice else [False]):
            try:
                daemon = self._factory(wv)  # type: ignore[misc]
            except BaseException as exc:  # ImportError, SystemExit (no voiceprint), OSError (no mic)...
                self._notify(on_state, "failed", str(exc) or type(exc).__name__)
                continue  # voice failed: carry on with the text assistant
            with self._lock:  # one lock for "was I cancelled?" and "register me": stop() cannot slip between
                cancelled = cancel.is_set()
                if not cancelled:
                    self._daemon, self._voice = daemon, wv
            if cancelled:
                daemon.stop()
                return
            self._notify(on_state, "ready", None)
            try:
                daemon.run()
            except BaseException as exc:
                self._notify(on_state, "stopped", str(exc) or type(exc).__name__)
            finally:
                with self._lock:
                    if self._daemon is daemon:
                        self._daemon, self._voice = None, False
            return

    @staticmethod
    def _notify(on_state: StateFn | None, kind: str, reason: str | None) -> None:
        if on_state:
            try:
                on_state(kind, reason)
            except Exception:
                pass

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._cancel.set()
            daemon, thread = self._daemon, self._thread
        if daemon is not None:
            daemon.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


class ServerHandle:
    def __init__(
        self,
        settings: Settings,
        host: str = "127.0.0.1",  # this PC only; phones are a deliberate opt-in via `jarvis serve`
        port: int = 8765,
        build: Callable[[], Any] | None = None,
        daemon_factory: Any = _DEFAULT,
    ) -> None:
        from .. import cli

        self.settings, self.host, self.port = settings, host, port
        self._build = build or (lambda: cli._make_jarvis(settings))
        if daemon_factory is _DEFAULT:
            daemon_factory = lambda with_voice: cli._make_daemon(settings, with_voice)  # noqa: E731
        self.runner = DaemonRunner(daemon_factory)
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    @property
    def token(self) -> str:
        from ..web.server import load_or_create_token

        return load_or_create_token(self.settings.data_dir / "web_token")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/#token={self.token}"

    def web_does_reflections(self) -> bool:
        """The assistant thinks ahead on schedule; the web server only steps in when it is not running."""
        return not self.runner.alive

    def start(self, timeout: float = 15.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        import uvicorn

        from ..web.server import create_app

        app = create_app(self._build, self.token, proactive=self.web_does_reflections)
        # log_config=None: uvicorn's default logging needs a console (stdout is None in a windowed exe).
        self._server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning", log_config=None))
        self._error = None

        def run() -> None:
            try:
                self._server.run()
            except BaseException as exc:  # uvicorn exits via SystemExit when the port is taken
                self._error = str(exc) or type(exc).__name__

        self._thread = threading.Thread(target=run, name="jarvis-web", daemon=True)
        self._thread.start()
        end = time.time() + timeout
        while time.time() < end:
            if self._server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        self._server.should_exit = True
        raise RuntimeError(f"웹 서버를 시작하지 못했습니다. 포트 {self.port}을(를) 다른 프로그램이 쓰고 있을 수 있습니다.")

    def stop(self) -> None:
        self.runner.stop()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(10)
