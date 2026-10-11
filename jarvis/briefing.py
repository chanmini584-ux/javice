"""Morning briefing: once a day, JARVIS summarizes where you stand and what to do next."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .memory import MemoryStore

BRIEFING_PROMPT = """\
아침 브리핑을 해줘. 소리 내어 읽어줄 거라서 표나 마크다운 없이 짧은 문장으로, 2분 안에 읽을 분량으로.
순서:
1. 오늘 날짜와 요일, 마감이 가까운 목표 (7일 이내는 꼭).
2. 오늘 할 가장 중요한 일 세 가지와 이유. 진행 중 계획의 다음 단계를 우선해.
3. 어제 내가 말했던 것 중 챙겨야 할 것 (아래 메모 참고).
4. 내 약점과 관련해 오늘 주의할 점이 있으면 한 가지.
필요하면 웹 검색으로 오늘 일정에 영향을 줄 정보를 확인해도 돼.

<recent_memos>
{memos}
</recent_memos>
"""


def briefing_prompt(store: MemoryStore, now: datetime) -> str:
    memos = store.memos(since=now - timedelta(days=1), limit=30)
    lines = "\n".join(f"- ({m.mentions}회 언급) {m.text}" for m in memos) or "(없음)"
    return BRIEFING_PROMPT.format(memos=lines)


def briefing_due(now: datetime, at: time, last_date: str | None) -> bool:
    """True once per day, at or after `at` local time."""
    return now.time() >= at and last_date != now.date().isoformat()


def parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))
