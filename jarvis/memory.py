"""Long-term memory: user profile, memories, goals and plans in SQLite.

This is what makes the agent *personal*: every turn, the profile, the user's
known weaknesses and active goals are injected into the system prompt, and the
model can read/write the rest through tools.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MEMORY_KINDS = ("fact", "preference", "weakness", "strength", "event", "insight")
GOAL_STATUSES = ("active", "done", "dropped")
STEP_STATUSES = ("todo", "doing", "done", "blocked")

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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Memory:
    id: int
    kind: str
    content: str
    importance: int
    created_at: str


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
