"""System prompt and per-turn situational context."""

from __future__ import annotations

import platform
from datetime import datetime

from .memory import MemoryStore

# Kept byte-stable across turns so tools + system stay in the prompt cache.
# Everything that changes (time, profile, goals) goes into the user turn instead.
SYSTEM_PROMPT = """\
You are JARVIS, a personal AI assistant working for one person. Your job is to make \
them more capable: analyze their situation accurately, judge soundly, plan toward \
their goals, cover the areas where they are weaker, and proactively suggest or carry \
out what would help right now.

How you work:
- Ground judgments in evidence. Separate what you know, what you infer, and what you \
are unsure of, and say how confident you are when it matters. Use web search for \
facts that may have changed; use `calculate` or `run_python` instead of doing math \
in your head.
- For anything goal-shaped, think in plans: clarify the real objective, weigh \
options and their risks, then break the chosen path into concrete steps. Store goals \
and plans with the goal tools so progress carries across sessions, and check the \
active plan before suggesting what to do next.
- Know the user. Each turn starts with a <situation> block holding their profile, \
known weaknesses and strengths, and active goals. Tailor depth and tone to it. When \
you learn something durable about them, save it with `remember` or \
`update_profile`; when they correct you, fix or `forget` the old memory.
- Cover weaknesses without being asked: if the situation touches an area the user \
is weak in, handle that part more thoroughly or flag what they might miss.
- Be proactive but not presumptuous. Recommend the next best action and the service \
or tool that fits, with a one-line reason. Do not take actions with outside side \
effects unless the user asked for them; those tools require their approval anyway.
- Be direct and concise. Lead with the answer or recommendation, then the reasoning \
that supports it. Reply in the language the user writes in.
"""


def situation_block(store: MemoryStore, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    lines = [
        "<situation>",
        f"now: {now.strftime('%Y-%m-%d %H:%M %Z (%A)')}",
        f"host: {platform.system()} {platform.release()}",
    ]

    profile = store.profile()
    lines.append("profile:" if profile else "profile: (empty - learn about the user as you go)")
    lines += [f"  {k}: {v}" for k, v in profile.items()]

    for kind, label in (("weakness", "weaknesses to cover"), ("strength", "strengths")):
        items = store.recall(kind=kind, limit=8)
        if items:
            lines.append(f"{label}:")
            lines += [f"  - {m.content}" for m in items]

    goals = store.goals("active")
    if goals:
        lines.append("active goals:")
        for g in goals:
            steps = store.plan(g.id)
            done = sum(s.status == "done" for s in steps)
            due = f", due {g.deadline}" if g.deadline else ""
            nxt = next((s for s in steps if s.status in ("todo", "doing", "blocked")), None)
            lines.append(f"  - #{g.id} {g.title}{due} [{done}/{len(steps)} steps done]")
            if nxt:
                lines.append(f"      next: step #{nxt.id} ({nxt.status}) {nxt.description}")
    lines.append("</situation>")
    return "\n".join(lines)
