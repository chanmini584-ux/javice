"""Knowledge base: collected documents in SQLite with full-text search.

FTS5's trigram tokenizer matches any 3+ character substring, which works for
Korean (no word segmentation needed) as well as English. Shorter terms fall back
to substring matching.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    published TEXT,
    fetched_at TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, text, content='documents', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
    INSERT INTO documents_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Document:
    id: int
    url: str
    source: str
    topic: str
    title: str
    published: str | None
    fetched_at: str
    text: str


class KnowledgeBase:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            # The daily collector writes from another thread/connection while the agent reads.
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def has(self, url: str) -> bool:
        return self._db.execute("SELECT 1 FROM documents WHERE url = ?", (url,)).fetchone() is not None

    def add(
        self,
        url: str,
        source: str,
        topic: str,
        title: str,
        text: str,
        published: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Insert or refresh a document. Returns True if it was new."""
        now = now or datetime.now(timezone.utc)
        new = not self.has(url)
        with self._db:
            self._db.execute(
                "INSERT INTO documents(url, source, topic, title, published, fetched_at, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                "title=excluded.title, text=excluded.text, fetched_at=excluded.fetched_at",
                (url, source, topic, title.strip(), _iso(published) if published else None, _iso(now), text.strip()),
            )
        return new

    def search(self, query: str, topic: str | None = None, limit: int = 8) -> list[tuple[Document, str]]:
        """Return (document, snippet) pairs, best match first."""
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        topic_sql = " AND d.topic = ?" if topic else ""
        long_terms = [t for t in terms if len(t) >= 3]
        if long_terms:
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            sql = (
                "SELECT d.*, snippet(documents_fts, 1, '[', ']', ' … ', 24) AS snip "
                "FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid "
                f"WHERE documents_fts MATCH ?{topic_sql} ORDER BY bm25(documents_fts, 3.0, 1.0) LIMIT ?"
            )
            params: list[object] = [match, *([topic] if topic else []), limit]
        else:
            like = " OR ".join(["(d.title LIKE ? OR d.text LIKE ?)"] * len(terms))
            sql = f"SELECT d.*, substr(d.text, 1, 160) AS snip FROM documents d WHERE ({like}){topic_sql} " \
                  "ORDER BY d.fetched_at DESC LIMIT ?"
            params = [p for t in terms for p in (f"%{t}%", f"%{t}%")] + ([topic] if topic else []) + [limit]
        rows = self._db.execute(sql, params).fetchall()
        return [(self._doc(r), r["snip"]) for r in rows]

    def recent(self, since: datetime, topic: str | None = None, limit: int = 30) -> list[Document]:
        sql = "SELECT * FROM documents WHERE fetched_at >= ?"
        params: list[object] = [_iso(since)]
        if topic:
            sql += " AND topic = ?"
            params.append(topic)
        sql += " ORDER BY COALESCE(published, fetched_at) DESC LIMIT ?"
        params.append(limit)
        return [self._doc(r) for r in self._db.execute(sql, params)]

    def prune(self, keep_days: int = 90, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
        with self._db:
            cur = self._db.execute("DELETE FROM documents WHERE fetched_at < ?", (_iso(cutoff),))
        return cur.rowcount

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM documents").fetchone()[0])

    @staticmethod
    def _doc(r: sqlite3.Row) -> Document:
        return Document(r["id"], r["url"], r["source"], r["topic"], r["title"], r["published"], r["fetched_at"], r["text"])
