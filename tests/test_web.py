"""Web API tests with FastAPI's TestClient and a stub agent (no network)."""

import json
import threading
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.agent import Jarvis  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.memory import MemoryStore  # noqa: E402
from jarvis.web.server import create_app, load_or_create_token  # noqa: E402

TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class EchoJarvis(Jarvis):
    """Streams a canned answer; numbers it by how many user turns the history holds."""

    def __init__(self):
        super().__init__(Settings(), store=MemoryStore(), client=object())
        self.threads = set()

    def ask(self, text):
        self.threads.add(threading.get_ident())
        if text == "boom":
            raise RuntimeError("API down")
        self.messages.append({"role": "user", "content": text})
        n = sum(m["role"] == "user" for m in self.messages)
        answer = f"{n}:{text}"
        self.on_event("tool", {"name": "calculate", "input": {}, "output": "private", "is_error": False})
        for part in (answer[:2], answer[2:]):
            self.on_event("text", {"text": part})
        self.messages.append({"role": "assistant", "content": answer})
        return NS(text=answer, stop_reason="end_turn", tool_calls=["calculate"])


@pytest.fixture
def client():
    built = []

    def build():
        built.append(EchoJarvis())
        return built[0]

    app = create_app(build, TOKEN)
    with TestClient(app) as c:
        c.built = built
        yield c


def post(client, path, body):
    return client.post(path, json=body, headers=AUTH).json()


def sse(client, body):
    with client.stream("POST", "/api/chat/stream", json=body, headers=AUTH) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        raw = "".join(r.iter_text())
    events = []
    for frame in raw.strip().split("\n\n"):
        kind = frame.split("\n")[0].removeprefix("event: ")
        events.append((kind, json.loads(frame.split("\n")[1].removeprefix("data: "))))
    return events


def test_auth_required(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/chat/stream", json={"message": "hi"}).status_code == 401
    assert client.get("/api/chats").status_code == 401
    assert client.get("/api/overview", headers={"Authorization": TOKEN}).status_code == 401


def test_conversations_are_saved_and_resumed(client):
    a = post(client, "/api/chat", {"message": "오늘 일정 정리해줘"})
    a2 = post(client, "/api/chat", {"message": "b", "chat_id": a["chat_id"]})
    other = post(client, "/api/chat", {"message": "c"})
    assert (a["text"], a2["text"], other["text"]) == ("1:오늘 일정 정리해줘", "2:b", "1:c")
    assert other["chat_id"] != a["chat_id"]

    listed = client.get("/api/chats", headers=AUTH).json()
    assert [c["title"] for c in listed] == ["c", "오늘 일정 정리해줘"]  # most recent first
    msgs = client.get(f"/api/chats/{a['chat_id']}", headers=AUTH).json()
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("user", "오늘 일정 정리해줘"), ("assistant", "1:오늘 일정 정리해줘"), ("user", "b"), ("assistant", "2:b")]
    assert msgs[1]["tools"] == ["calculate"]

    client.app.state.worker._live.clear()  # as after a restart: history comes back from the transcript
    assert post(client, "/api/chat", {"message": "d", "chat_id": a["chat_id"]})["text"] == "3:d"

    jarvis = client.built[0]
    assert len(client.built) == 1 and len(jarvis.threads) == 1
    assert threading.get_ident() not in jarvis.threads  # all agent work off the event loop


def test_stream_sends_deltas_tools_and_done(client):
    events = sse(client, {"message": "hello"})
    kinds = [k for k, _ in events]
    assert kinds == ["chat", "tool", "text", "text", "done"]
    chat_id = events[0][1]["chat_id"]
    assert events[1][1] == {"name": "calculate"}  # tool output is never sent to the browser
    assert "".join(d["text"] for k, d in events if k == "text") == "1:hello"
    assert events[-1][1]["text"] == "1:hello" and events[-1][1]["chat_id"] == chat_id
    assert sse(client, {"message": "again", "chat_id": chat_id})[-1][1]["text"] == "2:again"


def test_stream_reports_errors_and_keeps_transcript_clean(client):
    events = sse(client, {"message": "boom"})
    assert events[-1] == ("error", {"message": "RuntimeError: API down"})
    chat_id = events[0][1]["chat_id"]
    assert client.get(f"/api/chats/{chat_id}", headers=AUTH).json() == []


def test_delete_chat(client):
    chat_id = post(client, "/api/chat", {"message": "x"})["chat_id"]
    assert client.delete(f"/api/chats/{chat_id}", headers=AUTH).json() == {"ok": True}
    assert client.get(f"/api/chats/{chat_id}", headers=AUTH).status_code == 404
    assert client.get("/api/chats", headers=AUTH).json() == []
    # a deleted or unknown id just starts a new conversation
    assert post(client, "/api/chat", {"message": "y", "chat_id": chat_id})["chat_id"] != chat_id


def test_memo_and_overview(client):
    r = client.post("/api/memo", json={"text": "내일 치과 예약"}, headers=AUTH).json()
    assert r["new"] is True
    again = client.post("/api/memo", json={"text": "내일 치과 예약 잊지 말기"}, headers=AUTH).json()
    assert again == {"id": r["id"], "new": False}
    store = client.built[0].store

    def seed():  # SQLite objects belong to the worker thread
        store.save_plan(store.add_goal("포트폴리오", "이직"), ["초안"])

    client.app.state.worker._pool.submit(seed).result()
    o = client.get("/api/overview", headers=AUTH).json()
    assert o["memos"][0]["source"] == "app" and o["memos"][0]["mentions"] == 2
    assert o["goals"][0]["plan"][0]["description"] == "초안"


def test_validation(client):
    assert client.post("/api/chat", json={"message": ""}, headers=AUTH).status_code == 422


def test_web_app_assets_served(client):
    html = client.get("/")
    assert html.status_code == 200 and "JARVIS" in html.text and "manifest.webmanifest" in html.text
    assert "/api/chat/stream" in html.text and "/api/chats" in html.text
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert "serviceWorker" in html.text and client.get("/sw.js").status_code == 200
    assert client.get("/icon.svg").headers["content-type"].startswith("image/svg")


def test_token_persists(tmp_path):
    p = tmp_path / "web_token"
    t = load_or_create_token(p)
    assert len(t) >= 24 and load_or_create_token(p) == t


def test_schedule_endpoints_and_reminders(client):
    from datetime import datetime, timedelta

    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    a = post(client, "/api/schedule", {"title": "치과", "start": soon, "location": "강남"})
    b = post(client, "/api/schedule", {"title": "여행", "start": later})
    assert a["all_day"] is False and b["all_day"] is True and b["remind_min"] == 0
    assert [i["title"] for i in client.get("/api/schedule", headers=AUTH).json()] == ["치과", "여행"]
    assert [i["id"] for i in client.get("/api/reminders", headers=AUTH).json()] == [a["id"]]

    r = client.patch(f"/api/schedule/{a['id']}", json={"status": "done"}, headers=AUTH)
    assert r.json()["status"] == "done"
    assert client.get("/api/reminders", headers=AUTH).json() == []
    assert [i["title"] for i in client.get("/api/schedule", headers=AUTH).json()] == ["여행"]

    assert client.post("/api/schedule", json={"title": "x", "start": "내일"}, headers=AUTH).status_code == 422
    assert client.patch("/api/schedule/999", json={"title": "x"}, headers=AUTH).status_code == 404
    assert client.patch(f"/api/schedule/{b['id']}", json={"status": "bogus"}, headers=AUTH).status_code == 422
    assert client.get("/api/schedule").status_code == 401


def test_notices_and_reflect_now(client):
    worker = client.app.state.worker

    def seed():
        from datetime import datetime, timezone

        store, at = worker.jarvis.store, datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
        store.add_notice("knowledge", 1, "읽을거리", "논문 하나.", now=at)
        store.add_notice("deadline", 3, "과제 마감", "오늘 시작하세요.", now=at)

    worker._pool.submit(seed).result()
    unread = client.get("/api/notices?unread=true", headers=AUTH).json()
    assert [n["title"] for n in unread] == ["과제 마감", "읽을거리"]  # same moment: most important first
    assert post(client, "/api/notices/read", {"ids": [unread[0]["id"]]}) == {"marked": 1}
    assert len(client.get("/api/notices?unread=true", headers=AUTH).json()) == 1
    assert post(client, "/api/notices/read", {}) == {"marked": 1}
    assert len(client.get("/api/notices", headers=AUTH).json()) == 2

    reflected = []
    worker._reflector = NS(run=lambda: reflected.append(1) or [{"id": 9, "title": "t"}])
    assert post(client, "/api/reflect", {}) == [{"id": 9, "title": "t"}] and reflected == [1]

    def fail():
        raise OSError("network down")

    worker._reflector = NS(run=fail)
    r = client.post("/api/reflect", headers=AUTH)
    assert r.status_code == 502 and "network down" in r.json()["detail"]
    assert client.get("/api/notices").status_code == 401


def test_serve_listen_hands_reflections_to_the_web_server_when_the_daemon_stops(monkeypatch, tmp_path, capsys):
    # `serve --listen`: the daemon thread does the scheduled reflections. If it dies (no voiceprint
    # enrolled, microphone gone) the web server must take over, and the reason must be shown.
    import jarvis.cli as cli

    release = threading.Event()

    class FakeDaemon:
        def run(self):
            release.wait(5)
            raise SystemExit("등록된 목소리가 없습니다.")

    seen = {}

    def fake_uvicorn_run(app, **kwargs):
        worker = app.state.worker
        worker._reflector = NS(due=lambda now: True, run=lambda now=None: ["notice"])
        seen["while_daemon_runs"] = worker.reflect_if_due()
        release.set()
        for t in threading.enumerate():
            if t.name == "jarvis-daemon":
                t.join(5)
        seen["after_daemon_stops"] = worker.reflect_if_due()
        worker.shutdown()

    monkeypatch.setattr(cli, "_ensure_api_key", lambda settings: None)
    monkeypatch.setattr(cli, "_make_daemon", lambda settings, with_voice: FakeDaemon())
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    cli.cmd_serve(Settings(data_dir=tmp_path), ["--listen"])
    assert seen == {"while_daemon_runs": [], "after_daemon_stops": ["notice"]}
    assert "등록된 목소리가 없습니다" in capsys.readouterr().err
