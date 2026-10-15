"""Schedule tools: JARVIS keeps the user's calendar itself (no external calendar needed)."""

from __future__ import annotations

from datetime import datetime

from ..memory import MemoryStore
from .registry import ToolError, ToolRegistry

WHEN = (
    "Local wall-clock time on the user's PC: 'YYYY-MM-DD HH:MM' for a timed item, "
    "'YYYY-MM-DD' for an all-day item. Resolve words like '내일' or '목요일' from the "
    "current time in the situation block; never include a UTC offset."
)


def register_schedule_tools(registry: ToolRegistry, store: MemoryStore, clock=datetime.now) -> None:
    @registry.tool(
        "add_schedule",
        "Add an appointment, deadline or event to the user's schedule. Use it whenever the "
        "user mentions something happening at a specific date or time, then confirm the "
        "date, weekday and time back to them in one short line. If the time is ambiguous "
        "(e.g. '3시' could be 03:00 or 15:00), pick the sensible one and say which.",
        {
            "title": {"type": "string", "description": "Short title, e.g. '치과 진료'."},
            "start": {"type": "string", "description": WHEN},
            "end": {"type": "string", "description": "Optional end time, same format as start."},
            "location": {"type": "string"},
            "notes": {"type": "string"},
            "remind_minutes": {
                "type": "integer",
                "description": "Minutes before start to remind the user (default 10; 0 = at start). "
                "Use more for things that need travel or preparation.",
            },
        },
        required=["title", "start"],
    )
    def add_schedule(
        title: str, start: str, end: str = "", location: str = "", notes: str = "", remind_minutes: int = 10
    ) -> str:
        try:
            item_id = store.add_schedule(title, start, end or None, location, notes, remind_minutes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        item = store.get_schedule(item_id)
        clashes = [
            i for i in store.schedule(item.start_dt, days=1)
            if i.id != item_id and not i.all_day and not item.all_day and i.start == item.start
        ]
        out = f"added {item.describe()} ({item.start_dt.strftime('%A')})"
        if clashes:
            out += "; same start time as: " + ", ".join(c.describe() for c in clashes)
        return out

    @registry.tool(
        "list_schedule",
        "List scheduled items from a date (default today) for N days (default 7).",
        {
            "from_date": {"type": "string", "description": "YYYY-MM-DD; default today."},
            "days": {"type": "integer", "description": "How many days to cover (1-366)."},
            "include_closed": {"type": "boolean", "description": "Also show done/cancelled items."},
        },
        required=[],
    )
    def list_schedule(from_date: str = "", days: int = 7, include_closed: bool = False) -> str:
        start = datetime.strptime(from_date, "%Y-%m-%d") if from_date else clock()
        items = store.schedule(start, days=max(1, min(int(days), 366)), include_closed=include_closed)
        if not items:
            return "no scheduled items in that range"
        return "\n".join(i.describe() + ("" if i.status == "scheduled" else f" [{i.status}]") for i in items)

    @registry.tool(
        "update_schedule",
        "Change a scheduled item: move it, rename it, change the reminder, or mark it done or "
        "cancelled (status). Only pass the fields that change.",
        {
            "id": {"type": "integer"},
            "title": {"type": "string"},
            "start": {"type": "string", "description": WHEN},
            "end": {"type": "string"},
            "location": {"type": "string"},
            "notes": {"type": "string"},
            "remind_minutes": {"type": "integer"},
            "status": {"type": "string", "enum": ["scheduled", "done", "cancelled"]},
        },
        required=["id"],
    )
    def update_schedule(id: int, remind_minutes: int | None = None, **fields) -> str:
        try:
            ok = store.update_schedule(id, remind_min=remind_minutes, **fields)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not ok:
            raise ToolError(f"no schedule item #{id}")
        item = store.get_schedule(id)
        return f"updated {item.describe()} [{item.status}]"
