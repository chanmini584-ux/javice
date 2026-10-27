"""Web API for PC control: settings switch, action log, and approvals asked over the stream."""

import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.agent import Jarvis  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.memory import MemoryStore  # noqa: E402
from jarvis.pc.tools import describe_action  # noqa: E402
from jarvis.web.server import create_app  # noqa: E402

TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class AskingJarvis(Jarvis):
    """Asks for approval the way the agent loop does, then reports what the owner said."""

    def __init__(self, data_dir):
        super().__init__(Settings(data_dir=data_dir), store=MemoryStore(), client=object())

    def ask(self, text):
        allowed = self.approve("pc_delete_files", {"paths": ["C:/Users/me/Documents/old.txt"]})
        answer = "allowed" if allowed else "declined"
        return NS(text=answer, stop_reason="end_turn", tool_calls=["pc_delete_files"])


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(timeout=5.0):
        app = create_app(lambda: AskingJarvis(tmp_path), TOKEN, approval_timeout=timeout)
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def parse(raw):
    events = []
    for frame in raw.strip().split("\n\n"):
        kind = frame.split("\n")[0].removeprefix("event: ")
        events.append((kind, json.loads(frame.split("\n")[1].removeprefix("data: "))))
    return events


def stream_in_thread(client, out):
    def run():
        with client.stream("POST", "/api/chat/stream", json={"message": "x"}, headers=AUTH) as r:
            out["events"] = parse("".join(r.iter_text()))

    t = threading.Thread(target=run)
    t.start()
    return t


def wait_pending(client, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        rows = client.get("/api/approvals", headers=AUTH).json()
        if rows:
            return rows
        time.sleep(0.02)
    raise AssertionError("no approval became pending")


# --- settings ---------------------------------------------------------------


def test_pc_endpoints_need_the_token(client):
    for method, path in [("get", "/api/pc/settings"), ("put", "/api/pc/settings"), ("get", "/api/pc/log"),
                         ("get", "/api/approvals"), ("post", "/api/approvals/abc")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_settings_default_to_off(client):
    cfg = client.get("/api/pc/settings", headers=AUTH).json()
    assert cfg["enabled"] is False and cfg["roots"] and isinstance(cfg["apps"], dict)


def test_settings_can_be_switched_on_and_off(client):
    on = client.put("/api/pc/settings", json={"enabled": True}, headers=AUTH)
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert client.get("/api/pc/settings", headers=AUTH).json()["enabled"] is True
    off = client.put("/api/pc/settings", json={"enabled": False}, headers=AUTH).json()
    assert off["enabled"] is False


def test_settings_roots_and_apps_update(client, tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = client.put("/api/pc/settings", json={"roots": [str(folder)], "apps": {"Editor": "ed.exe"}}, headers=AUTH).json()
    assert cfg["roots"] == [str(folder.resolve())] and cfg["apps"] == {"editor": "ed.exe"}


@pytest.mark.parametrize("body", [{"roots": []}, {"roots": ["  "]}, {"roots": "C:/"}, {"apps": {"a": 1}}, {"enabled": "yes"}])
def test_settings_reject_bad_input(client, body):
    assert client.put("/api/pc/settings", json=body, headers=AUTH).status_code == 422


def test_settings_ignore_unknown_fields_and_keep_untouched_ones(client):
    client.put("/api/pc/settings", json={"enabled": True}, headers=AUTH)
    cfg = client.put("/api/pc/settings", json={"apps": {"x": "x.exe"}}, headers=AUTH).json()
    assert cfg["enabled"] is True  # a partial update leaves the switch alone


# --- log --------------------------------------------------------------------


def test_log_lists_newest_first_with_limit(client):
    pc = client.app.state.worker.jarvis.pc
    for i in range(5):
        pc.audit.record("pc_open_app", {"name": f"a{i}"}, "ok")
    rows = client.get("/api/pc/log?limit=3", headers=AUTH).json()
    assert [r["input"]["name"] for r in rows] == ["a4", "a3", "a2"]


def test_log_limit_is_clamped(client):
    assert client.get("/api/pc/log?limit=0", headers=AUTH).status_code == 200
    assert client.get("/api/pc/log?limit=100000", headers=AUTH).status_code == 200


# --- approvals --------------------------------------------------------------


def test_approval_is_asked_on_the_stream_and_allow_lets_it_run(client):
    out = {}
    t = stream_in_thread(client, out)
    pending = wait_pending(client)[0]
    assert pending["tool"] == "pc_delete_files" and "old.txt" in pending["summary"]
    res = client.post(f"/api/approvals/{pending['id']}", json={"allow": True}, headers=AUTH)
    assert res.status_code == 200
    t.join(5)
    kinds = [k for k, _ in out["events"]]
    assert "approval" in kinds and kinds[-1] == "done"
    assert out["events"][-1][1]["text"] == "allowed"
    approval = next(d for k, d in out["events"] if k == "approval")
    assert approval["id"] == pending["id"] and "old.txt" in approval["summary"]


def test_deny_blocks_the_action(client):
    out = {}
    t = stream_in_thread(client, out)
    pending = wait_pending(client)[0]
    client.post(f"/api/approvals/{pending['id']}", json={"allow": False}, headers=AUTH)
    t.join(5)
    assert out["events"][-1][1]["text"] == "declined"


def test_unanswered_approval_times_out_as_a_no(make_client):
    c = make_client(timeout=0.3)
    r = c.post("/api/chat", json={"message": "x"}, headers=AUTH)
    assert r.json()["text"] == "declined"
    assert c.get("/api/approvals", headers=AUTH).json() == []


def test_answering_unknown_or_used_approval_is_404(client):
    assert client.post("/api/approvals/nope", json={"allow": True}, headers=AUTH).status_code == 404
    out = {}
    t = stream_in_thread(client, out)
    pending = wait_pending(client)[0]
    assert client.post(f"/api/approvals/{pending['id']}", json={"allow": True}, headers=AUTH).status_code == 200
    t.join(5)
    assert client.post(f"/api/approvals/{pending['id']}", json={"allow": True}, headers=AUTH).status_code == 404


def test_approval_requires_an_explicit_boolean(client):
    out = {}
    t = stream_in_thread(client, out)
    pending = wait_pending(client)[0]
    assert client.post(f"/api/approvals/{pending['id']}", json={}, headers=AUTH).status_code == 422
    assert client.post(f"/api/approvals/{pending['id']}", json={"allow": "yes"}, headers=AUTH).status_code == 422
    client.post(f"/api/approvals/{pending['id']}", json={"allow": False}, headers=AUTH)
    t.join(5)


# --- plain-language summaries -----------------------------------------------


def test_describe_action_is_readable_for_the_risky_tools():
    assert "old.txt" in describe_action("pc_delete_files", {"paths": ["C:/x/old.txt", "C:/x/b.txt"]})
    assert "2" in describe_action("pc_delete_files", {"paths": ["a", "b"]})
    assert "weird-tool" in describe_action("pc_open_app", {"name": "weird-tool"})
    assert "https://shop.example.com/checkout" in describe_action("pc_open_url", {"url": "https://shop.example.com/checkout"})
    assert "print(1)" in describe_action("run_python", {"code": "print(1)"})


def test_describe_action_falls_back_and_never_raises():
    assert "mystery" in describe_action("mystery", {"a": 1})
    assert describe_action("pc_delete_files", {"paths": None})
    assert len(describe_action("run_python", {"code": "x" * 5000})) < 700


# --- the installable web app has the screens -------------------------------


def test_app_has_pc_settings_screen_and_approval_prompt(client):
    html = client.get("/").text
    assert 'data-view="pc"' in html and 'id="pcEnabled"' in html and 'id="pcLog"' in html
    assert "/api/pc/settings" in html and "/api/pc/log" in html
    # the prompt: handled on the stream, answered by the owner, safe default is "no"
    assert 'kind === "approval"' in html and "/api/approvals/" in html
    assert 'role="alertdialog"' in html and 'id="apNo"' in html


def test_app_never_puts_server_text_into_html_for_the_pc_screens(client):
    html = client.get("/").text
    start = html.index("// ---------- PC control")
    end = html.index("</script>", start)
    assert "innerHTML" not in html[start:end]


def test_service_worker_cache_was_bumped(client):
    assert "jarvis-shell-v3" in client.get("/sw.js").text
