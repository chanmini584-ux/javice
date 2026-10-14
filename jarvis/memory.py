"""Long-term memory: user profile, memories, goals and plans in SQLite.

This is what makes the agent *personal*: every turn, the profile, the user's
known weaknesses and active goals are injected into the system prompt, and the
model can read/write the rest through tools.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY_KINDS = ("fact", "preference", "weakness", "strength", "event", "insight")
GOAL_STATUSES = ("active", "done", "dropped")
STEP_STATUSES = ("todo", "doing", "done", "blocked")
MEMO_STATUSES = ("pending", "kept", "promoted")
SCHEDULE_STATUSES = ("scheduled", "done", "cancelled")
# A reminder still fires this long after the start time (e.g. the PC was asleep), never later.
REMINDER_GRACE = timedelta(minutes=10)
# The longest reminder lead time (the web form's limit too); larger values are capped.
MAX_REMIND_MIN = 7 * 24 * 60

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
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start TEXT NOT NULL,          -- local wall time: 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for all day
    end TEXT,
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    remind_min INTEGER NOT NULL DEFAULT 10,
    reminded_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedule_start ON schedule(status, start);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);
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


def local_naive(dt: datetime) -> datetime:
    """Schedule times are the user's wall-clock time on this PC."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _lead(minutes) -> int:
    """A reminder lead time within 0..MAX_REMIND_MIN (rows saved before the cap included)."""
    return max(0, min(int(minutes), MAX_REMIND_MIN))


def parse_when(value: str) -> tuple[str, bool]:
    """Normalize '2026-10-15', '2026-10-15 15:00' or ISO '2026-10-15T15:00:00' -> (text, all_day).

    Seconds and any UTC offset are dropped: the time is read as the user's local wall time.
    """
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2}))?", value or "")
    if not m:
        raise ValueError(f"unrecognized date/time {value!r}; use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'")
    date, hour, minute = m.groups()
    dt = datetime.strptime(f"{date} {hour or 0}:{minute or 0}", "%Y-%m-%d %H:%M")  # validates ranges
    return (dt.strftime("%Y-%m-%d"), True) if hour is None else (dt.strftime("%Y-%m-%d %H:%M"), False)


@dataclass
class ScheduleItem:
    id: int
    title: str
    start: str
    end: str | None
    location: str
    notes: str
    remind_min: int
    reminded_at: str | None
    status: str
    created_at: str

    @property
    def all_day(self) -> bool:
        return len(self.start) == 10

    @property
    def start_dt(self) -> datetime:
        return datetime.strptime(self.start, "%Y-%m-%d" if self.all_day else "%Y-%m-%d %H:%M")

    def describe(self) -> str:
        when = f"{self.start} (all day)" if self.all_day else self.start + (f"~{self.end[-5:]}" if self.end else "")
        extra = f" @ {self.location}" if self.location else ""
        return f"#{self.id} {when} {self.title}{extra}"


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
        if deadline:  # stored as YYYY-MM-DD: deadlines are compared and sorted as text
            deadline = parse_when(deadline)[0][:10]
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

    # --- schedule (JARVIS's own calendar) ---------------------------------

    def add_schedule(
        self, title: str, start: str, end: str | None = None, location: str = "", notes: str = "", remind_min: int = 10
    ) -> int:
        start_text, all_day = parse_when(start)
        end_text = parse_when(end)[0] if end else None
        if end_text and end_text < start_text:
            raise ValueError("end is before start")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO schedule(title, start, end, location, notes, remind_min, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title.strip(), start_text, end_text, location, notes, 0 if all_day else _lead(remind_min), _now()),
            )
        return int(cur.lastrowid)

    def get_schedule(self, item_id: int) -> ScheduleItem | None:
        row = self._db.execute("SELECT * FROM schedule WHERE id = ?", (item_id,)).fetchone()
        return ScheduleItem(**dict(row)) if row else None

    def schedule(self, start: datetime, days: int = 7, include_closed: bool = False) -> list[ScheduleItem]:
        """Items whose start falls on the local dates [start.date(), start.date() + days)."""
        first = local_naive(start).date()
        last = first + timedelta(days=days)
        sql = "SELECT * FROM schedule WHERE start >= ? AND start < ?"
        if not include_closed:
            sql += " AND status = 'scheduled'"
        rows = self._db.execute(sql + " ORDER BY start, id", (first.isoformat(), last.isoformat()))
        return [ScheduleItem(**dict(r)) for r in rows]

    def update_schedule(self, item_id: int, **fields) -> bool:
        item = self.get_schedule(item_id)
        if item is None:
            return False
        updates: dict = {}
        for key in ("title", "location", "notes"):
            if fields.get(key) is not None:
                updates[key] = fields[key]
        if fields.get("start"):
            updates["start"], all_day = parse_when(fields["start"])
            updates["reminded_at"] = None  # moved: remind again
            if all_day:
                updates["remind_min"] = 0
        if fields.get("end"):
            updates["end"] = parse_when(fields["end"])[0]
        if fields.get("remind_min") is not None:
            updates["remind_min"] = _lead(fields["remind_min"])
            updates["reminded_at"] = None
        if fields.get("status") is not None:
            if fields["status"] not in SCHEDULE_STATUSES:
                raise ValueError(f"status must be one of {SCHEDULE_STATUSES}")
            updates["status"] = fields["status"]
        if not updates:
            return True
        cols = ", ".join(f"{k} = ?" for k in updates)
        with self._db:
            self._db.execute(f"UPDATE schedule SET {cols} WHERE id = ?", (*updates.values(), item_id))
        return True

    def reminders_due(self, now: datetime, include_reminded: bool = False) -> list[ScheduleItem]:
        """Timed items inside their reminder window: start - remind_min <= now <= start + grace."""
        now = local_naive(now)
        # Look as far ahead as the longest reminder lead time (they can be days ahead).
        (lead,) = self._db.execute(
            "SELECT COALESCE(MAX(remind_min), 0) FROM schedule WHERE status = 'scheduled'"
        ).fetchone()
        first, last = now - REMINDER_GRACE, now + timedelta(minutes=_lead(lead))
        items = self.schedule(first, days=(last.date() - first.date()).days + 1)
        due = []
        for item in items:
            if item.all_day or (item.reminded_at and not include_reminded):
                continue
            start = item.start_dt
            if start - timedelta(minutes=_lead(item.remind_min)) <= now <= start + REMINDER_GRACE:
                due.append(item)
        return due

    def mark_reminded(self, item_id: int, now: datetime | None = None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE schedule SET reminded_at = ? WHERE id = ?", ((now or datetime.now()).isoformat(), item_id)
            )

    # --- notices: things JARVIS decided on its own to tell the user ----------

    def add_notice(self, kind: str, priority: int, title: str, message: str, now: datetime | None = None) -> int:
        ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO notices(kind, priority, title, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, max(1, min(3, int(priority))), title.strip(), message.strip(), ts),
            )
        return int(cur.lastrowid)

    def notices(self, unread_only: bool = False, limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM notices" + (" WHERE read_at IS NULL" if unread_only else "")
        # newest batch first; inside a batch, the most important first
        order = " ORDER BY created_at DESC, priority DESC, id"
        return [dict(r) for r in self._db.execute(sql + order + " LIMIT ?", (limit,))]

    def mark_notices_read(self, ids: list[int] | None = None) -> int:
        now = _now()
        with self._db:
            if ids is None:
                return self._db.execute("UPDATE notices SET read_at = ? WHERE read_at IS NULL", (now,)).rowcount
            marks = ",".join("?" * len(ids)) or "NULL"
            return self._db.execute(
                f"UPDATE notices SET read_at = ? WHERE read_at IS NULL AND id IN ({marks})", (now, *ids)
            ).rowcount

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
