"""Long-term memory: user profile, memories, goals and plans in SQLite.

This is what makes the agent *personal*: every turn, the profile, the user's
known weaknesses and active goals are injected into the system prompt, and the
model can read/write the rest through tools.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY_KINDS = ("fact", "preference", "weakness", "strength", "event", "insight")
GOAL_STATUSES = ("active", "done", "dropped")
STEP_STATUSES = ("todo", "doing", "done", "blocked")
MEMO_STATUSES = ("pending", "kept", "promoted")

# How long a voice memo survives without being mentioned again, by importance.
# None = keep until JARVIS or the user removes it. Re-mentioning resets the clock.
MEMO_TTL_DAYS: dict[int, float | None] = {1: 1, 2: 3, 3: 7, 4: 30, 5: None}
# Two memos whose character-bigram Jaccard similarity reaches this are "the same topic".
REMENTION_SIMILARITY = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo'
);
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'voice',
    importance INTEGER,
    mentions INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    tools TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat_id, id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bigrams(text: str) -> set[str]:
    t = "".join(text.lower().split())
    return {t[i : i + 2] for i in range(len(t) - 1)} or {t}


def similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity; language-agnostic, works for Korean."""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x | y else 0.0


@dataclass
class Memory:
    id: int
    kind: str
    content: str
    importance: int
    created_at: str


@dataclass
class Memo:
    id: int
    text: str
    source: str
    importance: int | None
    mentions: int
    status: str
    created_at: str
    last_seen_at: str


@dataclass
class Goal:
    id: int
    title: str
    why: str
    deadline: str | None
    status: str
    created_at: str


@dataclass
class PlanStep:
    id: int
    goal_id: int
    idx: int
    description: str
    status: str


class MemoryStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # The daemon, the web server and the CLI may hold connections at the same time.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- profile -----------------------------------------------------------

    def set_profile(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO profile(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def profile(self) -> dict[str, str]:
        rows = self._db.execute("SELECT key, value FROM profile ORDER BY key")
        return {r["key"]: r["value"] for r in rows}

    # --- memories ----------------------------------------------------------

    def remember(self, kind: str, content: str, importance: int = 3) -> int:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"kind must be one of {MEMORY_KINDS}")
        importance = max(1, min(5, int(importance)))
        with self._db:
            cur = self._db.execute(
                "INSERT INTO memories(kind, content, importance, created_at) VALUES (?, ?, ?, ?)",
                (kind, content.strip(), importance, _now()),
            )
        return int(cur.lastrowid)

    def forget(self, memory_id: int) -> bool:
        with self._db:
            cur = self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    def recall(self, query: str = "", kind: str | None = None, limit: int = 10) -> list[Memory]:
        """Rank memories by how many query terms they contain, then importance.

        Substring matching (instead of FTS tokenization) keeps Korean working,
        where particles attach to words ("목표를", "목표가").
        """
        terms = [t for t in query.split() if t]
        score_sql = " + ".join(["(instr(lower(content), lower(?)) > 0)"] * len(terms)) or "0"
        sql = f"SELECT *, ({score_sql}) AS score FROM memories"
        params: list[object] = list(terms)
        where = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if terms:
            where.append(f"({score_sql}) > 0")
            params.extend(terms)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score DESC, importance DESC, id DESC LIMIT ?"
        params.append(limit)
        return [
            Memory(r["id"], r["kind"], r["content"], r["importance"], r["created_at"])
            for r in self._db.execute(sql, params)
        ]

    # --- goals & plans -----------------------------------------------------

    def add_goal(self, title: str, why: str = "", deadline: str | None = None) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO goals(title, why, deadline, created_at) VALUES (?, ?, ?, ?)",
                (title.strip(), why.strip(), deadline, _now()),
            )
        return int(cur.lastrowid)

    def set_goal_status(self, goal_id: int, status: str) -> bool:
        if status not in GOAL_STATUSES:
            raise ValueError(f"status must be one of {GOAL_STATUSES}")
        with self._db:
            cur = self._db.execute("UPDATE goals SET status = ? WHERE id = ?", (status, goal_id))
        return cur.rowcount > 0

    def goals(self, status: str | None = "active") -> list[Goal]:
        sql, params = "SELECT * FROM goals", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(deadline, '9999'), id"
        return [
            Goal(r["id"], r["title"], r["why"], r["deadline"], r["status"], r["created_at"])
            for r in self._db.execute(sql, params)
        ]

    def save_plan(self, goal_id: int, steps: list[str]) -> list[int]:
        """Replace the plan for a goal with a fresh ordered list of steps."""
        if not self._db.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone():
            raise ValueError(f"no goal with id {goal_id}")
        with self._db:
            self._db.execute("DELETE FROM plan_steps WHERE goal_id = ?", (goal_id,))
            ids = [
                int(
                    self._db.execute(
                        "INSERT INTO plan_steps(goal_id, idx, description) VALUES (?, ?, ?)",
                        (goal_id, i, s.strip()),
                    ).lastrowid
                )
                for i, s in enumerate(steps, start=1)
            ]
        return ids

    def plan(self, goal_id: int) -> list[PlanStep]:
        rows = self._db.execute(
            "SELECT * FROM plan_steps WHERE goal_id = ? ORDER BY idx", (goal_id,)
        )
        return [PlanStep(r["id"], r["goal_id"], r["idx"], r["description"], r["status"]) for r in rows]

    def set_step_status(self, step_id: int, status: str) -> bool:
        if status not in STEP_STATUSES:
            raise ValueError(f"status must be one of {STEP_STATUSES}")
        with self._db:
            cur = self._db.execute("UPDATE plan_steps SET status = ? WHERE id = ?", (status, step_id))
        return cur.rowcount > 0

    # --- voice memos -------------------------------------------------------

    def add_memo(self, text: str, source: str = "voice", now: datetime | None = None) -> tuple[int, bool]:
        """Store what the user said. If it repeats a recent memo, count a re-mention instead.

        Returns (memo_id, is_new).
        """
        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        text = text.strip()
        best: tuple[float, Memo] | None = None
        for memo in self.memos(limit=200):
            score = similarity(text, memo.text)
            if score >= REMENTION_SIMILARITY and (best is None or score > best[0]):
                best = (score, memo)
        with self._db:
            if best:
                self._db.execute(
                    # A re-mentioned "kept" memo goes back to JARVIS for re-judgment.
                    "UPDATE memos SET mentions = mentions + 1, last_seen_at = ?, "
                    "status = CASE WHEN status = 'kept' THEN 'pending' ELSE status END WHERE id = ?",
                    (ts, best[1].id),
                )
                return best[1].id, False
            cur = self._db.execute(
                "INSERT INTO memos(text, source, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (text, source, ts, ts),
            )
        return int(cur.lastrowid), True

    def memos(self, status: str | None = None, limit: int = 50, since: datetime | None = None) -> list[Memo]:
        sql, params = "SELECT * FROM memos", []
        where = []
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("last_seen_at >= ?")
            params.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [Memo(**dict(r)) for r in self._db.execute(sql, params)]

    def update_memo(self, memo_id: int, importance: int | None = None, status: str | None = None) -> bool:
        if status is not None and status not in MEMO_STATUSES:
            raise ValueError(f"status must be one of {MEMO_STATUSES}")
        sets, params = [], []
        if importance is not None:
            sets.append("importance = ?")
            params.append(max(1, min(5, int(importance))))
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return False
        with self._db:
            cur = self._db.execute(f"UPDATE memos SET {', '.join(sets)} WHERE id = ?", (*params, memo_id))
        return cur.rowcount > 0

    def delete_memo(self, memo_id: int) -> bool:
        with self._db:
            cur = self._db.execute("DELETE FROM memos WHERE id = ?", (memo_id,))
        return cur.rowcount > 0

    def prune_memos(self, now: datetime | None = None) -> list[int]:
        """Delete judged memos that outlived their importance-based TTL. Pending ones wait for judgment."""
        now = now or datetime.now(timezone.utc)
        doomed = []
        for memo in self.memos(limit=10_000):
            if memo.status == "pending" or memo.importance is None:
                continue
            ttl = MEMO_TTL_DAYS.get(memo.importance)
            if ttl is None:
                continue
            # Each extra mention buys more time: the user keeps coming back to it.
            ttl *= memo.mentions
            if datetime.fromisoformat(memo.last_seen_at) + timedelta(days=ttl) < now:
                doomed.append(memo.id)
        with self._db:
            self._db.executemany("DELETE FROM memos WHERE id = ?", [(i,) for i in doomed])
        return doomed

    # --- sensor events (vision, later other devices) ------------------------

    def add_event(self, source: str, kind: str, detail: str, at: datetime | None = None) -> int:
        ts = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO events(source, kind, detail, at) VALUES (?, ?, ?, ?)", (source, kind, detail, ts)
            )
        return int(cur.lastrowid)

    def events(self, since: datetime | None = None, limit: int = 20) -> list[dict]:
        sql, params = "SELECT * FROM events", []
        if since:
            sql += " WHERE at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._db.execute(sql, params)]

    # --- chat transcripts (what the chat app shows; not long-term memory) ------

    def new_chat(self, title: str) -> int:
        now = _now()
        title = " ".join(title.split())
        title = title if len(title) <= 40 else title[:39] + "…"
        with self._db:
            cur = self._db.execute(
                "INSERT INTO chats(title, created_at, updated_at) VALUES (?, ?, ?)", (title or "새 대화", now, now)
            )
        return int(cur.lastrowid)

    def chats(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute("SELECT * FROM chats ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def chat_exists(self, chat_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is not None

    def add_chat_turn(self, chat_id: int, user: str, assistant: str, tools: list[str] = ()) -> None:
        now = _now()
        with self._db:
            self._db.executemany(
                "INSERT INTO chat_messages(chat_id, role, text, tools, created_at) VALUES (?, ?, ?, ?, ?)",
                [(chat_id, "user", user, "", now), (chat_id, "assistant", assistant, ",".join(tools), now)],
            )
            self._db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))

    def chat_messages(self, chat_id: int) -> list[dict]:
        rows = self._db.execute(
            "SELECT role, text, tools, created_at FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        )
        return [{**dict(r), "tools": [t for t in r["tools"].split(",") if t]} for r in rows]

    def delete_chat(self, chat_id: int) -> bool:
        with self._db:
            return self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    # --- internal key/value state (not shown to the model) ----------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
