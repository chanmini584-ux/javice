"""Desktop app core: start with Windows, the app window, server lifecycle, voice on/off.

Everything Windows-specific is injected, so these run anywhere; what only a real Windows PC
can prove (registry, tray, installer) is listed in the README under "not verified".
"""

import json
import ntpath
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from jarvis.agent import Jarvis  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.desktop.autostart import Autostart, WinRegistry, launch_command  # noqa: E402
from jarvis.desktop.launcher import already_running, find_app_browser, open_ui  # noqa: E402
from jarvis.desktop.server import DaemonRunner, ServerHandle  # noqa: E402
from jarvis.memory import MemoryStore  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- start with Windows -----------------------------------------------------


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


CMD = '"C:\\Program Files\\JARVIS\\JARVIS.exe" --background'


def test_autostart_enable_and_disable():
    reg = FakeRegistry()
    a = Autostart(CMD, registry=reg)
    assert a.available and not a.is_enabled()
    a.enable()
    assert a.is_enabled() and reg.values == {"JARVIS": CMD}
    a.disable()
    assert not a.is_enabled() and reg.values == {}
    a.disable()  # already off: not an error


def test_autostart_enable_replaces_a_stale_path():
    reg = FakeRegistry()
    reg.values["JARVIS"] = '"D:\\old\\JARVIS.exe" --background'
    Autostart(CMD, registry=reg).enable()
    assert reg.values["JARVIS"] == CMD


def test_autostart_without_a_registry_is_unavailable_and_refuses():
    a = Autostart(CMD, registry=None)
    assert a.available is False and a.is_enabled() is False
    with pytest.raises(RuntimeError, match="Windows"):
        a.enable()


def test_launch_command_for_the_installed_exe_quotes_the_path():
    cmd = launch_command(frozen=True, executable="C:\\Program Files\\JARVIS\\JARVIS.exe")
    assert cmd == CMD


def test_launch_command_from_source_prefers_pythonw_so_no_console_opens():
    cmd = launch_command(frozen=False, executable="C:\\Py\\python.exe", exists=lambda p: p.endswith("pythonw.exe"))
    assert cmd == '"C:\\Py\\pythonw.exe" -m jarvis.desktop --background'
    cmd = launch_command(frozen=False, executable="C:\\Py\\python.exe", exists=lambda p: False)
    assert cmd == '"C:\\Py\\python.exe" -m jarvis.desktop --background'


@pytest.mark.skipif(sys.platform == "win32", reason="the real registry exists here")
def test_real_registry_wrapper_refuses_off_windows():
    with pytest.raises(RuntimeError):
        WinRegistry().get("JARVIS")


# --- the app window ---------------------------------------------------------

ENV = {"ProgramFiles": "C:\\Program Files", "ProgramFiles(x86)": "C:\\Program Files (x86)", "LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"}
EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"


def test_edge_is_preferred_then_chrome_then_none():
    assert find_app_browser(exists=lambda p: p in (EDGE, CHROME), env=ENV, join=ntpath.join) == EDGE
    assert find_app_browser(exists=lambda p: p == CHROME, env=ENV, join=ntpath.join) == CHROME
    assert find_app_browser(exists=lambda p: False, env=ENV, join=ntpath.join) is None


def test_open_ui_uses_a_chromeless_app_window_so_it_feels_like_an_app():
    spawned = []
    open_ui("http://127.0.0.1:8765/#token=abc", finder=lambda: EDGE, spawn=lambda args: spawned.append(args), fallback=lambda u: 1 / 0)
    assert spawned and spawned[0][0] == EDGE and "--app=http://127.0.0.1:8765/#token=abc" in spawned[0]


def test_open_ui_falls_back_to_the_default_browser():
    opened = []
    open_ui("http://x/", finder=lambda: None, spawn=lambda a: 1 / 0, fallback=opened.append)
    assert opened == ["http://x/"]

    opened.clear()

    def boom(_):
        raise OSError("cannot start")

    open_ui("http://x/", finder=lambda: EDGE, spawn=boom, fallback=opened.append)
    assert opened == ["http://x/"]


class _Health(BaseHTTPRequestHandler):
    body = {"status": "ok"}

    def do_GET(self):
        data = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(body):
    handler = type("H", (_Health,), {"body": body})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_already_running_detects_a_running_jarvis_only():
    jarvis = _serve({"status": "ok"})
    other = _serve({"hello": "world"})
    try:
        assert already_running(jarvis.server_address[1]) is True
        assert already_running(other.server_address[1]) is False
    finally:
        jarvis.shutdown()
        other.shutdown()
    assert already_running(free_port()) is False


# --- server lifecycle -------------------------------------------------------


class QuietJarvis(Jarvis):
    def __init__(self, data_dir):
        super().__init__(Settings(data_dir=data_dir), store=MemoryStore(), client=object())


def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
        return json.load(r)


def test_server_starts_serves_and_stops(tmp_path):
    port = free_port()
    h = ServerHandle(Settings(data_dir=tmp_path), port=port, build=lambda: QuietJarvis(tmp_path), daemon_factory=None)
    h.start()
    try:
        assert health(port) == {"status": "ok"}
        token = (tmp_path / "web_token").read_text().strip()
        assert h.url == f"http://127.0.0.1:{port}/#token={token}"
        h.start()  # a second start is harmless
        assert health(port) == {"status": "ok"}
    finally:
        h.stop()
    assert already_running(port) is False


def test_server_only_listens_on_this_pc(tmp_path):
    h = ServerHandle(Settings(data_dir=tmp_path), port=free_port(), build=lambda: QuietJarvis(tmp_path), daemon_factory=None)
    assert h.host == "127.0.0.1"


def test_port_in_use_gives_a_clear_error(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        h = ServerHandle(Settings(data_dir=tmp_path), port=port, build=lambda: QuietJarvis(tmp_path), daemon_factory=None)
        with pytest.raises(RuntimeError, match=str(port)):
            h.start()


# --- background assistant and voice ----------------------------------------


class FakeDaemon:
    def __init__(self, with_voice):
        self.with_voice = with_voice
        self._stop = threading.Event()
        self.stopped = False
        self.ran = threading.Event()

    def run(self):
        self.ran.set()
        self._stop.wait(10)

    def stop(self):
        self.stopped = True
        self._stop.set()


def make_runner(created, fail=None):
    def factory(with_voice):
        if fail and with_voice:
            raise fail
        d = FakeDaemon(with_voice)
        created.append(d)
        return d

    return DaemonRunner(factory)


def test_background_assistant_runs_without_voice_by_default():
    created, states = [], []
    r = make_runner(created)
    r.start(with_voice=False, on_state=lambda k, why: states.append(k))
    assert wait_for(lambda: created and created[0].ran.is_set())
    assert r.alive and not r.voice_active and created[0].with_voice is False
    r.stop()
    assert created[0].stopped and not r.alive


def test_voice_can_be_switched_on_and_off_while_running():
    created, states = [], []
    r = make_runner(created)
    r.start(with_voice=False)
    assert wait_for(lambda: created and created[0].ran.is_set())
    r.start(with_voice=True, on_state=lambda k, why: states.append(k))  # restarts with voice
    assert wait_for(lambda: len(created) == 2 and created[1].ran.is_set())
    assert created[0].stopped and created[1].with_voice and r.voice_active and "ready" in states
    r.start(with_voice=False)
    assert wait_for(lambda: len(created) == 3 and created[2].ran.is_set())
    assert created[1].stopped and not r.voice_active
    r.stop()


@pytest.mark.parametrize("error", [ImportError("No module named torch"), SystemExit("등록된 목소리가 없습니다."), OSError("no microphone")])
def test_voice_failure_is_reported_and_text_assistant_keeps_running(error):
    created, states = [], []
    r = make_runner(created, fail=error)
    r.start(with_voice=False)
    assert wait_for(lambda: created and created[0].ran.is_set())
    r.start(with_voice=True, on_state=lambda k, why: states.append((k, why)))
    assert wait_for(lambda: any(k == "failed" for k, _ in states))
    reason = next(w for k, w in states if k == "failed")
    assert reason  # something the user can read
    assert wait_for(lambda: len(created) == 2 and created[1].with_voice is False and created[1].ran.is_set())
    assert r.alive and not r.voice_active
    r.stop()


def test_stop_is_safe_when_never_started():
    DaemonRunner(lambda v: FakeDaemon(v)).stop()


def test_web_server_takes_over_reflections_only_when_no_assistant_runs(tmp_path):
    created = []
    runner = make_runner(created)
    h = ServerHandle(Settings(data_dir=tmp_path), port=free_port(), build=lambda: QuietJarvis(tmp_path), daemon_factory=None)
    h.runner = runner
    assert h.web_does_reflections() is True
    runner.start(with_voice=False)
    assert wait_for(lambda: runner.alive)
    assert h.web_does_reflections() is False
    runner.stop()
    assert h.web_does_reflections() is True
