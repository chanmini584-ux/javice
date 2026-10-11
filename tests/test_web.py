"""Web API tests with FastAPI's TestClient and a stub agent (no network)."""

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
    """Records which conversation and thread each turn ran on."""

    def __init__(self):
        super().__init__(Settings(), store=MemoryStore(), client=object())
        self.threads = set()

    def ask(self, text):
        self.threads.add(threading.get_ident())
        self.messages.append({"role": "user", "content": text})
        return NS(text=f"{len(self.messages)}:{text}", stop_reason="end_turn", tool_calls=[])


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


def test_auth_required(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/overview", headers={"Authorization": TOKEN}).status_code == 401


def test_chat_keeps_separate_conversations_on_one_thread(client):
    r1 = client.post("/api/chat", json={"message": "a", "session": "phone"}, headers=AUTH).json()
    r2 = client.post("/api/chat", json={"message": "b", "session": "phone"}, headers=AUTH).json()
    r3 = client.post("/api/chat", json={"message": "c", "session": "laptop"}, headers=AUTH).json()
    assert (r1["text"], r2["text"], r3["text"]) == ("1:a", "2:b", "1:c")
    client.post("/api/reset", json={"session": "phone"}, headers=AUTH)
    assert client.post("/api/chat", json={"message": "d", "session": "phone"}, headers=AUTH).json()["text"] == "1:d"
    jarvis = client.built[0]
    assert len(client.built) == 1 and len(jarvis.threads) == 1
    assert threading.get_ident() not in jarvis.threads  # all agent work off the event loop


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
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert "serviceWorker" in html.text and client.get("/sw.js").status_code == 200
    assert client.get("/icon.svg").headers["content-type"].startswith("image/svg")


def test_token_persists(tmp_path):
    p = tmp_path / "web_token"
    t = load_or_create_token(p)
    assert len(t) >= 24 and load_or_create_token(p) == t
