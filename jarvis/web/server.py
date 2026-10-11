"""Web server: a JSON API plus an installable web app (PWA) for any browser or phone.

The phone does not run JARVIS; it is a window onto the JARVIS running on a PC
(at home, or wherever the USB stick is plugged in). Every API call needs the
access token generated on first start.

All JARVIS work happens on one dedicated worker thread: SQLite connections are
single-threaded, and the conversation state is not meant for concurrent turns.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ..agent import Jarvis

STATIC = resources.files("jarvis.web") / "static"
MAX_SESSIONS = 20


def load_or_create_token(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # e.g. FAT-formatted USB sticks have no permissions
    return token


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session: str = Field(default="default", max_length=64)


class SessionIn(BaseModel):
    session: str = Field(default="default", max_length=64)


class MemoIn(BaseModel):
    text: str = Field(min_length=1, max_length=5_000)


class Worker:
    """Owns the Jarvis instance on a single thread; one conversation per client session."""

    def __init__(self, build: Callable[[], Jarvis]) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-web")
        self._jarvis: Jarvis | None = None
        self._build = build
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    @property
    def jarvis(self) -> Jarvis:
        if self._jarvis is None:  # created lazily, on the worker thread
            self._jarvis = self._build()
        return self._jarvis

    async def run(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    def chat(self, session: str, message: str) -> dict[str, Any]:
        j = self.jarvis
        if session not in self._sessions and len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))  # drop the oldest conversation
        j.messages = self._sessions.setdefault(session, [])
        result = j.ask(message)
        return {"text": result.text, "stop_reason": result.stop_reason, "tools": result.tool_calls}

    def reset(self, session: str) -> None:
        self._sessions.pop(session, None)

    def overview(self) -> dict[str, Any]:
        store = self.jarvis.store
        return {
            "profile": store.profile(),
            "goals": [{**asdict(g), "plan": [asdict(s) for s in store.plan(g.id)]} for g in store.goals()],
            "memos": [asdict(m) for m in store.memos(limit=30)],
            "events": store.events(since=datetime.now(timezone.utc) - timedelta(days=1), limit=20),
        }

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)


def create_app(build: Callable[[], Jarvis], token: str) -> FastAPI:
    worker = Worker(build)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        worker.shutdown()

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.worker = worker

    def auth(authorization: str = Header(default="")) -> None:
        scheme, _, given = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/chat", dependencies=[Depends(auth)])
    async def chat(body: ChatIn) -> dict[str, Any]:
        return await worker.run(lambda: worker.chat(body.session, body.message))

    @app.post("/api/reset", dependencies=[Depends(auth)])
    async def reset(body: SessionIn) -> dict[str, bool]:
        await worker.run(lambda: worker.reset(body.session))
        return {"ok": True}

    @app.post("/api/memo", dependencies=[Depends(auth)])
    async def memo(body: MemoIn) -> dict[str, Any]:
        # Typed or dictated on the user's own device, so it is trusted as the owner's words.
        memo_id, is_new = await worker.run(lambda: worker.jarvis.store.add_memo(body.text, source="app"))
        return {"id": memo_id, "new": is_new}

    @app.get("/api/overview", dependencies=[Depends(auth)])
    async def overview() -> dict[str, Any]:
        return await worker.run(worker.overview)

    # --- the installable web app -------------------------------------------------

    def static(name: str, media_type: str, no_cache: bool = False) -> Response:
        data = (STATIC / name).read_bytes()
        headers = {"Cache-Control": "no-cache"} if no_cache else {}
        return Response(data, media_type=media_type, headers=headers)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return static("index.html", "text/html; charset=utf-8", no_cache=True)

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return static("manifest.webmanifest", "application/manifest+json")

    @app.get("/sw.js")
    async def service_worker() -> Response:
        return static("sw.js", "text/javascript", no_cache=True)

    @app.get("/icon.svg")
    async def icon() -> Response:
        return static("icon.svg", "image/svg+xml")

    return app


__all__ = ["create_app", "load_or_create_token"]
