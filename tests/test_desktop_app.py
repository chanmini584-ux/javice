"""Desktop app shell: tray actions and menu, API-key onboarding, and the start-up flow."""

import os

os.environ.setdefault("PYSTRAY_BACKEND", "dummy")  # lets the tray menu be built without a screen

import pytest

pytest.importorskip("fastapi")

from jarvis.config import Settings  # noqa: E402
from jarvis.desktop.app import parse_args, run, setup_stdio  # noqa: E402
from jarvis.desktop.keys import ensure_api_key  # noqa: E402
from jarvis.desktop.tray import TrayActions  # noqa: E402
from jarvis.pc.store import PcSettingsStore  # noqa: E402


# --- fakes ------------------------------------------------------------------


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.voice_active = False
        self.alive = False
        self.on_state = None

    def start(self, with_voice=False, on_state=None):
        self.calls.append(("start", with_voice))
        self.on_state = on_state
        self.alive = True
        self.voice_active = with_voice

    def stop(self, timeout=5.0):
        self.calls.append(("stop",))
        self.alive = False


class FakeHandle:
    def __init__(self, start_error=None):
        self.url = "http://127.0.0.1:8765/#token=abc"
        self.runner = FakeRunner()
        self.events = []
        self.start_error = start_error

    def start(self):
        self.events.append("start")
        if self.start_error:
            raise self.start_error

    def stop(self):
        self.events.append("stop")


class FakeAutostart:
    def __init__(self, available=True, fail=None):
        self.available = available
        self.on = False
        self.fail = fail

    def is_enabled(self):
        return self.on

    def enable(self):
        if self.fail:
            raise self.fail
        self.on = True

    def disable(self):
        self.on = False


@pytest.fixture
def parts(tmp_path):
    notes, opened, stopped = [], [], []
    store = PcSettingsStore(tmp_path / "pc.json", home=tmp_path)
    handle = FakeHandle()
    auto = FakeAutostart()
    actions = TrayActions(
        handle, auto, store,
        notify=lambda title, msg: notes.append((title, msg)),
        open_window=opened.append,
        stop_tray=lambda: stopped.append(1),
    )
    return actions, handle, auto, store, notes, opened, stopped


# --- tray actions -----------------------------------------------------------


def test_open_shows_the_app_window(parts):
    actions, handle, _, _, _, opened, _ = parts
    actions.open()
    assert opened == [handle.url]


def test_pc_control_switch_from_the_tray_is_the_same_switch_as_in_settings(parts):
    actions, _, _, store, *_ = parts
    assert actions.pc_on is False
    actions.toggle_pc_control()
    assert actions.pc_on is True and store.load().enabled is True
    actions.toggle_pc_control()
    assert store.load().enabled is False


def test_turning_pc_control_on_tells_the_owner_what_it_allows(parts):
    actions, *_, notes, _, _ = parts
    actions.toggle_pc_control()
    assert notes and "PC 제어" in notes[-1][0]


def test_autostart_toggle(parts):
    actions, _, auto, *_ = parts
    assert actions.autostart_on is False
    actions.toggle_autostart()
    assert auto.on and actions.autostart_on
    actions.toggle_autostart()
    assert not auto.on


def test_autostart_unavailable_or_failing_is_explained_not_raised(parts):
    actions, _, auto, _, notes, *_ = parts
    auto.available = False
    actions.toggle_autostart()
    assert "Windows" in notes[-1][1]
    auto.available, auto.fail = True, OSError("access denied")
    actions.toggle_autostart()
    assert "access denied" in notes[-1][1] and not auto.on


def test_voice_on_restarts_the_assistant_with_voice_and_reports_ready(parts):
    actions, handle, _, _, notes, *_ = parts
    actions.toggle_voice()
    assert handle.runner.calls == [("start", True)]
    handle.runner.on_state("ready", None)
    assert "음성" in notes[-1][0] and "켜" in notes[-1][1]


def test_voice_failure_says_why_in_plain_words(parts):
    actions, handle, _, _, notes, *_ = parts
    actions.toggle_voice()
    handle.runner.on_state("failed", "No module named 'torch'")
    title, msg = notes[-1]
    assert "음성" in title and "torch" in msg


def test_voice_off_returns_to_the_text_assistant(parts):
    actions, handle, *_ = parts
    handle.runner.voice_active = True
    actions.toggle_voice()
    assert handle.runner.calls == [("start", False)]


def test_quit_stops_the_tray(parts):
    actions, *_, stopped = parts
    actions.quit()
    assert stopped == [1]


# --- the tray menu ----------------------------------------------------------


def test_menu_has_the_expected_entries_and_reflects_state(parts):
    pytest.importorskip("pystray")
    from jarvis.desktop.tray import build_menu

    actions, _, auto, store, *_ = parts
    menu = build_menu(actions)
    items = [i for i in menu.items if i.text and not set(i.text) <= set("- ")]
    assert [i.text for i in items] == ["JARVIS 열기", "음성 모드", "PC 제어 허용", "Windows 시작 시 자동 실행", "종료"]
    state = {i.text: i.checked for i in items}
    assert state["PC 제어 허용"] is False and state["Windows 시작 시 자동 실행"] is False
    store.update(enabled=True)
    auto.on = True
    state = {i.text: i.checked for i in build_menu(actions).items if i.text}
    assert state["PC 제어 허용"] is True and state["Windows 시작 시 자동 실행"] is True


def test_the_default_tray_click_opens_the_app(parts):
    pytest.importorskip("pystray")
    from jarvis.desktop.tray import build_menu

    menu = build_menu(parts[0])
    assert next(i for i in menu.items if i.default).text == "JARVIS 열기"


def test_icon_is_an_image_of_the_requested_size():
    PIL = pytest.importorskip("PIL")  # noqa: F841
    from jarvis.desktop.tray import make_icon

    img = make_icon(64)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert make_icon(256).size == (256, 256)


# --- API key ----------------------------------------------------------------


def test_existing_key_is_never_asked_again(tmp_path):
    asked = []
    ok = ensure_api_key(Settings(data_dir=tmp_path), ask_key=lambda: asked.append(1), ask_save=lambda: True, env={"ANTHROPIC_API_KEY": "k"})
    assert ok and asked == []


def test_key_is_asked_once_and_saved_when_the_owner_agrees(tmp_path):
    env = {}
    ok = ensure_api_key(Settings(data_dir=tmp_path), ask_key=lambda: "  sk-ant-123  ", ask_save=lambda: True, env=env)
    assert ok and env["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert (tmp_path / "secrets.env").read_text() == "ANTHROPIC_API_KEY=sk-ant-123\n"


def test_key_is_not_written_to_disk_without_consent(tmp_path):
    env = {}
    assert ensure_api_key(Settings(data_dir=tmp_path), ask_key=lambda: "sk-1", ask_save=lambda: False, env=env)
    assert env["ANTHROPIC_API_KEY"] == "sk-1" and not (tmp_path / "secrets.env").exists()


@pytest.mark.parametrize("given", [None, "", "   ", "two words", "line\nbreak", "a=b"])
def test_missing_or_malformed_key_is_rejected(tmp_path, given):
    env = {}
    assert ensure_api_key(Settings(data_dir=tmp_path), ask_key=lambda: given, ask_save=lambda: True, env=env) is False
    assert "ANTHROPIC_API_KEY" not in env and not (tmp_path / "secrets.env").exists()


# --- output without a console ----------------------------------------------


def test_windowed_exe_without_stdout_gets_a_log_file(tmp_path):
    out, err = setup_stdio(tmp_path, None, None)
    out.write("hello\n")
    out.flush()
    assert "hello" in (tmp_path / "logs" / "desktop.log").read_text(encoding="utf-8")
    assert err is not None


def test_real_streams_are_left_alone(tmp_path):
    marker_out, marker_err = object(), object()
    assert setup_stdio(tmp_path, marker_out, marker_err) == (marker_out, marker_err)


# --- start-up flow ----------------------------------------------------------


def test_parse_args_defaults():
    a = parse_args([])
    assert a.background is False and a.port == 8765
    assert parse_args(["--background", "--port", "9000"]).background is True


class Flow:
    """Wires fakes into run() and records what happened."""

    def __init__(self, tmp_path, running=False, key=True, start_error=None, tray_error=None):
        self.handle = FakeHandle(start_error)
        self.windows, self.errors, self.tray_runs = [], [], []
        self.running, self.key, self.tray_error = running, key, tray_error
        self.settings = Settings(data_dir=tmp_path)

    def go(self, argv=()):
        def tray(actions):
            self.tray_runs.append(actions)
            if self.tray_error:
                raise self.tray_error

        return run(
            list(argv),
            settings=self.settings,
            handle_factory=lambda s, port: self.handle,
            run_tray=tray,
            ensure_key=lambda s: self.key,
            running=lambda port: self.running,
            open_window=self.windows.append,
            show_error=lambda title, msg: self.errors.append((title, msg)),
            autostart=FakeAutostart(),
        )


def test_normal_start_opens_the_window_runs_the_tray_then_cleans_up(tmp_path):
    f = Flow(tmp_path)
    assert f.go() == 0
    assert f.handle.events == ["start", "stop"]
    assert f.handle.runner.calls == [("start", False)]  # background assistant on, voice off
    assert f.windows == [f.handle.url] and len(f.tray_runs) == 1


def test_background_start_stays_in_the_tray_without_a_window(tmp_path):
    f = Flow(tmp_path)
    assert f.go(["--background"]) == 0
    assert f.windows == [] and len(f.tray_runs) == 1


def test_second_launch_just_shows_the_running_app(tmp_path):
    f = Flow(tmp_path, running=True)
    assert f.go() == 0
    assert f.windows == [f.handle.url] and f.handle.events == [] and f.tray_runs == []


def test_second_background_launch_does_nothing(tmp_path):
    f = Flow(tmp_path, running=True)
    assert f.go(["--background"]) == 0
    assert f.windows == [] and f.handle.events == []


def test_no_api_key_means_a_clear_message_and_no_server(tmp_path):
    f = Flow(tmp_path, key=False)
    assert f.go() == 1
    assert f.errors and "API" in f.errors[0][1] and f.handle.events == []


def test_server_that_cannot_start_is_reported(tmp_path):
    f = Flow(tmp_path, start_error=RuntimeError("포트 8765을(를) 쓸 수 없습니다"))
    assert f.go() == 1
    assert "8765" in f.errors[0][1] and f.handle.runner.calls == [] and f.windows == []


def test_a_broken_tray_never_leaves_an_invisible_background_process(tmp_path):
    f = Flow(tmp_path, tray_error=ImportError("no tray"))
    assert f.go() == 1
    assert f.handle.events[-1] == "stop" and f.errors


# --- always really exit -----------------------------------------------------


def test_exit_now_flushes_then_ends_the_process_even_with_stray_threads(monkeypatch, capsys):
    import sys

    from jarvis.desktop import app

    codes = []
    monkeypatch.setattr(app.os, "_exit", codes.append)
    flushed = []

    class Stream:
        def flush(self):
            flushed.append(1)

    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", Stream())
    app.exit_now(3)
    assert codes == [3] and len(flushed) == 2


def test_module_entry_point_uses_exit_now():
    import pathlib

    import jarvis.desktop as pkg

    assert "exit_now" in (pathlib.Path(pkg.__file__).parent / "__main__.py").read_text(encoding="utf-8")


def test_no_tray_mode_runs_headless_until_interrupted(tmp_path):
    f = Flow(tmp_path)
    waited = []
    from jarvis.desktop.app import run as run_app

    rc = run_app(
        ["--no-tray"],
        settings=f.settings,
        handle_factory=lambda s, port: f.handle,
        run_tray=lambda a: pytest.fail("tray must not start"),
        ensure_key=lambda s: True,
        running=lambda port: False,
        open_window=f.windows.append,
        show_error=lambda t, m: f.errors.append((t, m)),
        autostart=FakeAutostart(),
        wait_forever=lambda: waited.append(1),
    )
    assert rc == 0 and waited == [1] and f.handle.events == ["start", "stop"]
