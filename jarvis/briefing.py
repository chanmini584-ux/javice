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
5. 새로 수집된 자료 중 내 관심사와 목표에 비춰 볼 만한 것 최대 3개, 한 줄씩. 필요하면 search_knowledge로 내용을 확인해.
6. 오늘 날씨 (프로필에 위치가 있으면 웹 검색으로).

<recent_memos>
{memos}
</recent_memos>

<new_articles>
{articles}
</new_articles>
"""


def briefing_prompt(store: MemoryStore, now: datetime, knowledge=None) -> str:
    memos = store.memos(since=now - timedelta(days=1), limit=30)
    lines = "\n".join(f"- ({m.mentions}회 언급) {m.text}" for m in memos) or "(없음)"
    articles = "(없음)"
    if knowledge is not None:
        from .knowledge import TOPICS

        docs = [d for t in TOPICS for d in knowledge.recent(now - timedelta(days=1), topic=t, limit=8)]
        if docs:
            articles = "\n".join(f"- [{d.topic}] {d.title} ({d.source})" for d in docs)
    return BRIEFING_PROMPT.format(memos=lines, articles=articles)


def briefing_due(now: datetime, at: time, last_date: str | None) -> bool:
    """True once per day, at or after `at` local time."""
    return now.time() >= at and last_date != now.date().isoformat()


def parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))
