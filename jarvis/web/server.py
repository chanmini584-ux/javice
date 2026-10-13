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
import json
import secrets
from contextlib import asynccontextmanager
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..agent import Jarvis
from ..memory import ScheduleItem

STATIC = resources.files("jarvis.web") / "static"
MAX_LIVE_CHATS = 20


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
    chat_id: int | None = None  # None starts a new conversation


class MemoIn(BaseModel):
    text: str = Field(min_length=1, max_length=5_000)


class ScheduleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(description="'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' (all day), local time")
    end: str | None = None
    location: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2_000)
    remind_min: int = Field(default=10, ge=0, le=7 * 24 * 60)


class ScheduleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: str | None = None
    end: str | None = None
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2_000)
    remind_min: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    status: str | None = None


Emit = Callable[[str, dict[str, Any]], None]


class Worker:
    """Owns the Jarvis instance on a single thread.

    Every conversation is saved as a transcript, so the chat list survives reloads
    and restarts. The live model history (with thinking blocks) is kept for the
    most recent conversations; an older one is resumed from its transcript.
    """

    def __init__(self, build: Callable[[], Jarvis]) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-web")
        self._jarvis: Jarvis | None = None
        self._build = build
        self._live: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()

    @property
    def jarvis(self) -> Jarvis:
        if self._jarvis is None:  # created lazily, on the worker thread
            self._jarvis = self._build()
        return self._jarvis

    async def run(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    def _history(self, chat_id: int) -> list[dict[str, Any]]:
        if chat_id not in self._live:
            if len(self._live) >= MAX_LIVE_CHATS:
                self._live.popitem(last=False)
            self._live[chat_id] = [
                {"role": m["role"], "content": m["text"]} for m in self.jarvis.store.chat_messages(chat_id)
            ]
        self._live.move_to_end(chat_id)
        return self._live[chat_id]

    def chat(self, chat_id: int | None, message: str, emit: Emit | None = None) -> dict[str, Any]:
        j, store = self.jarvis, self.jarvis.store
        if chat_id is None or not store.chat_exists(chat_id):
            chat_id = store.new_chat(message)
        if emit:
            emit("chat", {"chat_id": chat_id})
        j.messages = self._history(chat_id)
        previous = j.on_event

        def forward(kind: str, data: dict[str, Any]) -> None:
            previous(kind, data)
            if emit is None:
                return
            if kind == "text":
                emit("text", {"text": data["text"]})
            elif kind in ("tool", "server_tool"):
                emit("tool", {"name": data["name"]})  # names only: outputs can be large or private

        j.on_event = forward
        try:
            result = j.ask(message)
        finally:
            j.on_event = previous
        store.add_chat_turn(chat_id, message, result.text, result.tool_calls)
        return {"chat_id": chat_id, "text": result.text, "stop_reason": result.stop_reason, "tools": result.tool_calls}

    def chats(self) -> list[dict[str, Any]]:
        return self.jarvis.store.chats()

    def messages(self, chat_id: int) -> list[dict[str, Any]] | None:
        store = self.jarvis.store
        return store.chat_messages(chat_id) if store.chat_exists(chat_id) else None

    def delete(self, chat_id: int) -> bool:
        self._live.pop(chat_id, None)
        return self.jarvis.store.delete_chat(chat_id)

    def schedule(self, days: int) -> list[dict[str, Any]]:
        now = datetime.now()
        return [_item(i) for i in self.jarvis.store.schedule(now, days=days)]

    def reminders(self) -> list[dict[str, Any]]:
        # Shown by every open browser; each one remembers what it already showed.
        return [_item(i) for i in self.jarvis.store.reminders_due(datetime.now(), include_reminded=True)]

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


def _item(item: ScheduleItem) -> dict[str, Any]:
    return {**asdict(item), "all_day": item.all_day}


def _sse(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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
        return await worker.run(lambda: worker.chat(body.chat_id, body.message))

    @app.post("/api/chat/stream", dependencies=[Depends(auth)])
    async def chat_stream(body: ChatIn) -> StreamingResponse:
        """Server-sent events: chat, text (deltas), tool, then done or error."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

        def emit(kind: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

        def job() -> None:
            try:
                emit("done", worker.chat(body.chat_id, body.message, emit))
            except Exception as e:  # surfaced to the chat window instead of a dropped connection
                emit("error", {"message": f"{type(e).__name__}: {e}"})

        task = asyncio.ensure_future(worker.run(job))  # start now; the generator only drains the queue

        async def events():
            while True:
                kind, data = await queue.get()
                yield _sse(kind, data)
                if kind in ("done", "error"):
                    break
            await task

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    @app.get("/api/chats", dependencies=[Depends(auth)])
    async def chats() -> list[dict[str, Any]]:
        return await worker.run(worker.chats)

    @app.get("/api/chats/{chat_id}", dependencies=[Depends(auth)])
    async def chat_messages(chat_id: int) -> list[dict[str, Any]]:
        found = await worker.run(lambda: worker.messages(chat_id))
        if found is None:
            raise HTTPException(status_code=404, detail="no such chat")
        return found

    @app.delete("/api/chats/{chat_id}", dependencies=[Depends(auth)])
    async def delete_chat(chat_id: int) -> dict[str, bool]:
        return {"ok": await worker.run(lambda: worker.delete(chat_id))}

    @app.post("/api/memo", dependencies=[Depends(auth)])
    async def memo(body: MemoIn) -> dict[str, Any]:
        # Typed or dictated on the user's own device, so it is trusted as the owner's words.
        memo_id, is_new = await worker.run(lambda: worker.jarvis.store.add_memo(body.text, source="app"))
        return {"id": memo_id, "new": is_new}

    @app.get("/api/schedule", dependencies=[Depends(auth)])
    async def schedule(days: int = 60) -> list[dict[str, Any]]:
        return await worker.run(lambda: worker.schedule(max(1, min(days, 366))))

    @app.post("/api/schedule", dependencies=[Depends(auth)])
    async def add_schedule(body: ScheduleIn) -> dict[str, Any]:
        def add() -> dict[str, Any]:
            store = worker.jarvis.store
            item_id = store.add_schedule(body.title, body.start, body.end, body.location, body.notes, body.remind_min)
            return _item(store.get_schedule(item_id))

        try:
            return await worker.run(add)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.patch("/api/schedule/{item_id}", dependencies=[Depends(auth)])
    async def update_schedule(item_id: int, body: ScheduleUpdate) -> dict[str, Any]:
        def update() -> dict[str, Any] | None:
            store = worker.jarvis.store
            if not store.update_schedule(item_id, **body.model_dump(exclude_none=True)):
                return None
            return _item(store.get_schedule(item_id))

        try:
            found = await worker.run(update)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if found is None:
            raise HTTPException(status_code=404, detail="no such item")
        return found

    @app.get("/api/reminders", dependencies=[Depends(auth)])
    async def reminders() -> list[dict[str, Any]]:
        return await worker.run(worker.reminders)

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
