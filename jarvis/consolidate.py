"""Memo consolidation: JARVIS judges what you said and turns it into memory and plans.

Pending voice memos are sent to Claude in one structured-output request. For each
memo it decides importance and whether to promote it to long-term memory, keep it
as a memo, or delete it; from the whole batch it extracts new goals with step plans
and progress on existing plan steps. Everything it touches is JARVIS's own memory,
so it is applied without asking. Memos then expire by importance unless you bring
them up again (see MEMO_TTL_DAYS).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent import FALLBACK_BETA
from .config import Settings
from .memory import MEMORY_KINDS, STEP_STATUSES, MemoryStore, similarity
from .prompts import situation_block

# A proposed goal this similar to an active goal is the same goal said again.
GOAL_REPEAT_SIMILARITY = 0.6

_SYSTEM = """\
You are the memory curator for JARVIS, a personal assistant. You receive notes \
transcribed from things the user said out loud during their day, plus their current \
profile, goals and plans. Decide what is worth keeping.

For each memo:
- importance 1-5: 1 = small talk or noise (including transcription garbage), 3 = \
useful for days, 5 = core to who they are or what they are trying to do.
- action "promote": a durable fact, preference, weakness, strength, event or insight \
about the user; write it as one self-contained sentence in memory_text with the \
right memory_kind.
- action "keep": time-bound or still unclear; it stays as a memo and expires unless \
mentioned again. A higher mentions count means the user keeps coming back to it.
- action "delete": no value.

Then look across all memos and the situation. If the user expressed an intention or \
commitment that is not yet a goal, add it to new_goals with a concrete, ordered plan. \
If something they said shows progress on an existing plan step, report it in \
step_updates. Do not invent goals the user did not express; when in doubt, keep the \
memo instead. Write memory_text, goals and steps in the user's language.
"""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memos", "new_goals", "step_updates"],
    "properties": {
        "memos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "importance", "action", "memory_kind", "memory_text"],
                "properties": {
                    "id": {"type": "integer"},
                    "importance": {"type": "integer"},
                    "action": {"type": "string", "enum": ["promote", "keep", "delete"]},
                    "memory_kind": {"type": "string", "enum": ["", *MEMORY_KINDS]},
                    "memory_text": {"type": "string"},
                },
            },
        },
        "new_goals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "why", "deadline", "steps"],
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD or empty"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "step_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step_id", "status"],
                "properties": {
                    "step_id": {"type": "integer"},
                    "status": {"type": "string", "enum": list(STEP_STATUSES)},
                },
            },
        },
    },
}


@dataclass
class ConsolidationReport:
    promoted: list[int] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    new_goals: list[int] = field(default_factory=list)
    step_updates: int = 0

    def summary(self) -> str:
        return (
            f"promoted {len(self.promoted)}, kept {len(self.kept)}, deleted {len(self.deleted)}, "
            f"expired {len(self.expired)}, new goals {len(self.new_goals)}, step updates {self.step_updates}"
        )


class Consolidator:
    def __init__(self, client: Any, store: MemoryStore, settings: Settings) -> None:
        self.client = client
        self.store = store
        self.settings = settings

    def run(self, now: datetime | None = None, batch: int = 100) -> ConsolidationReport:
        now = now or datetime.now(timezone.utc)
        report = ConsolidationReport()
        pending = list(reversed(self.store.memos(status="pending", limit=batch)))  # oldest first
        if pending:
            self._apply(self._judge(pending), {m.id for m in pending}, report)
        report.expired = self.store.prune_memos(now)
        return report

    def _judge(self, pending: list) -> dict[str, Any]:
        memo_lines = "\n".join(
            json.dumps(
                {"id": m.id, "said_at": m.created_at, "mentions": m.mentions, "text": m.text},
                ensure_ascii=False,
            )
            for m in pending
        )
        response = self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=16000,
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"{situation_block(self.store)}\n\n<memos>\n{memo_lines}\n</memos>",
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"consolidation stopped early: {response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def _apply(self, decision: dict[str, Any], pending_ids: set[int], report: ConsolidationReport) -> None:
        store = self.store
        for d in decision.get("memos", []):
            memo_id = d["id"]
            if memo_id not in pending_ids:  # ignore ids the model made up
                continue
            importance = max(1, min(5, int(d["importance"])))
            if d["action"] == "delete":
                store.delete_memo(memo_id)
                report.deleted.append(memo_id)
            elif d["action"] == "promote" and d["memory_kind"] in MEMORY_KINDS and d["memory_text"].strip():
                store.remember(d["memory_kind"], d["memory_text"], importance)
                store.update_memo(memo_id, importance=importance, status="promoted")
                report.promoted.append(memo_id)
            else:
                store.update_memo(memo_id, importance=importance, status="kept")
                report.kept.append(memo_id)
        for g in decision.get("new_goals", []):
            title = g["title"].strip()
            if not title or any(similarity(title, a.title) >= GOAL_REPEAT_SIMILARITY for a in store.goals("active")):
                continue  # empty, or an active goal said again
            try:
                goal_id = store.add_goal(title, g["why"], g["deadline"] or None)
            except ValueError:  # a deadline that is not a date ("이번 달 말"): keep the goal without it
                goal_id = store.add_goal(title, g["why"], None)
            steps = [s for s in g["steps"] if s.strip()]
            if steps:
                store.save_plan(goal_id, steps)
            report.new_goals.append(goal_id)
        for u in decision.get("step_updates", []):
            if store.set_step_status(u["step_id"], u["status"]):
                report.step_updates += 1
