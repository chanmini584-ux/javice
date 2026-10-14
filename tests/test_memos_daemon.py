"""Voice memos, consolidation and the always-on daemon, with fake models."""

import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np

from jarvis.agent import Jarvis
from jarvis.briefing import briefing_due
from jarvis.config import Settings
from jarvis.consolidate import Consolidator
from jarvis.daemon import LAST_BRIEFING_KEY, Daemon
from jarvis.memory import MemoryStore
from jarvis.voice.base import Utterance

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def test_remention_bumps_count_and_requeues_kept_memo():
    store = MemoryStore()
    a, new = store.add_memo("다음 주 금요일까지 보고서 써야 해", now=T0)
    assert new
    store.update_memo(a, importance=3, status="kept")
    b, new = store.add_memo("금요일까지 보고서 써야 하는데", now=T0 + timedelta(hours=2))
    assert (b, new) == (a, False)
    memo = store.memos()[0]
    assert memo.mentions == 2 and memo.status == "pending"  # goes back for re-judgment
    _, new = store.add_memo("점심은 김치찌개", now=T0)
    assert new


def test_prune_by_importance_ttl_and_mentions():
    store = MemoryStore()
    low, _ = store.add_memo("ㅋㅋ 웃기다", now=T0)
    mid, _ = store.add_memo("세탁소에 셔츠 맡김", now=T0)
    core, _ = store.add_memo("나는 아침형 인간이 되고 싶다", now=T0)
    pending, _ = store.add_memo("아직 판단 안 된 메모", now=T0)
    store.update_memo(low, importance=1, status="kept")
    store.update_memo(mid, importance=3, status="kept")
    store.update_memo(core, importance=5, status="promoted")
    assert store.prune_memos(T0 + timedelta(days=2)) == [low]
    assert store.prune_memos(T0 + timedelta(days=8)) == [mid]
    assert {m.id for m in store.memos()} == {core, pending}


def test_memos_since_handles_local_timezones():
    store = MemoryStore()
    store.add_memo("old", now=T0 - timedelta(days=3))
    store.add_memo("recent", now=T0)
    kst = timezone(timedelta(hours=9))
    since = (T0 - timedelta(hours=1)).astimezone(kst)
    assert [m.text for m in store.memos(since=since)] == ["recent"]


class FakeClient:
    def __init__(self, decision):
        self.decision = decision
        self.params = None
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **params):
        self.params = params
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(self.decision, ensure_ascii=False))])


def test_consolidator_applies_decisions():
    store = MemoryStore()
    gid = store.add_goal("영어 공부", "이직")
    (step_id,) = store.save_plan(gid, ["단어 100개"])
    m1, _ = store.add_memo("난 숫자 계산할 때 실수가 잦아", now=T0)
    m2, _ = store.add_memo("음 그러니까", now=T0)
    m3, _ = store.add_memo("이번 달 안에 블로그 하나 만들고 싶다", now=T0)
    m4, _ = store.add_memo("오늘 단어 100개 다 외웠다", now=T0)
    decision = {
        "memos": [
            {"id": m1, "importance": 4, "action": "promote", "memory_kind": "weakness", "memory_text": "계산 실수가 잦다"},
            {"id": m2, "importance": 1, "action": "delete", "memory_kind": "", "memory_text": ""},
            {"id": m3, "importance": 4, "action": "keep", "memory_kind": "", "memory_text": ""},
            {"id": m4, "importance": 2, "action": "keep", "memory_kind": "", "memory_text": ""},
            {"id": 9999, "importance": 5, "action": "delete", "memory_kind": "", "memory_text": ""},
        ],
        "new_goals": [{"title": "블로그 개설", "why": "기록", "deadline": "2026-10-31", "steps": ["플랫폼 고르기", "첫 글"]}],
        "step_updates": [{"step_id": step_id, "status": "done"}],
    }
    client = FakeClient(decision)
    report = Consolidator(client, store, Settings()).run(now=T0)
    assert report.promoted == [m1] and report.deleted == [m2] and report.kept == [m3, m4]
    assert store.recall(kind="weakness")[0].content == "계산 실수가 잦다"
    assert {g.title for g in store.goals()} == {"영어 공부", "블로그 개설"}
    assert len(store.plan(report.new_goals[0])) == 2
    assert store.plan(gid)[0].status == "done"
    assert client.params["output_config"]["format"]["type"] == "json_schema"
    assert "<memos>" in client.params["messages"][0]["content"]


def test_consolidator_skips_repeated_goals_and_keeps_goals_with_vague_deadlines():
    store = MemoryStore()
    store.add_goal("영어 공부", "이직")
    store.add_goal("정보처리산업기사 필기 합격", "취업")
    m, _ = store.add_memo("영어 공부 진짜 해야지. 실기도 준비하고, 블로그도 이번 달 안에", now=T0)
    decision = {
        "memos": [{"id": m, "importance": 3, "action": "keep", "memory_kind": "", "memory_text": ""}],
        "new_goals": [
            {"title": "영어 공부", "why": "이직", "deadline": "", "steps": ["단어"]},  # an active goal again
            {"title": "정보처리산업기사 실기 합격", "why": "취업", "deadline": "", "steps": ["기출"]},  # different
            {"title": "블로그 개설", "why": "기록", "deadline": "이번 달 말", "steps": []},  # not a date
            {"title": "블로그  개설", "why": "기록", "deadline": "", "steps": []},  # twice in one batch
        ],
        "step_updates": [],
    }
    report = Consolidator(FakeClient(decision), store, Settings()).run(now=T0)
    assert [(g.title, g.deadline) for g in store.goals()] == [
        ("영어 공부", None), ("정보처리산업기사 필기 합격", None), ("정보처리산업기사 실기 합격", None), ("블로그 개설", None)
    ]
    assert len(report.new_goals) == 2 and report.repeated_goals == ["영어 공부", "블로그  개설"]


class Recorder:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


class StubJarvis(Jarvis):
    def __init__(self, store, settings):
        super().__init__(settings, store=store, client=object())
        self.asked = []
        self.readonly = []

    def ask(self, text, readonly=False):
        self.asked.append(text)
        self.readonly.append(readonly)
        return NS(text=f"answer to: {text[:20]}")


class StubConsolidator:
    def __init__(self):
        self.runs = 0

    def run(self, now=None):
        self.runs += 1
        return NS(summary=lambda: "ok")


def utt(text, owner=True):
    return Utterance(audio=np.zeros(1), started_at=T0, is_owner=owner, owner_score=0.9 if owner else 0.1, text=text)


def make_daemon(clock, **settings):
    store = MemoryStore()
    jarvis = StubJarvis(store, Settings(**settings))
    speaker, cons = Recorder(), StubConsolidator()
    return Daemon(jarvis, None, speaker, cons, clock=clock, log=lambda s: None), jarvis, speaker, cons


def test_daemon_memos_commands_and_strangers():
    now = [T0]
    d, jarvis, speaker, _ = make_daemon(lambda: now[0], briefing_time="")
    d.handle(utt("우유 사야 해"))
    d.handle(utt("남의 말", owner=False))
    d.handle(utt("자비스, 오늘 일정 알려줘"))
    assert [m.text for m in d.store.memos()] == ["우유 사야 해"]
    assert d.others_heard == 1
    assert "오늘 일정 알려줘" in jarvis.asked[0] and speaker.said


def test_voice_conversation_starts_fresh_after_a_quiet_gap():
    # Without this the always-on conversation grows all day (more tokens and cost on every command).
    now = [T0]
    d, jarvis, _, _ = make_daemon(lambda: now[0], briefing_time="")
    d.handle(utt("자비스, 내일 날씨 어때?"))
    jarvis.messages = ["earlier turns"]
    now[0] += timedelta(minutes=10)
    d.handle(utt("자비스, 그럼 우산 챙길까?"))  # a follow-up keeps the context
    assert jarvis.messages == ["earlier turns"]
    now[0] += timedelta(minutes=31)
    d.handle(utt("자비스, 오늘 할 일 알려줘"))
    assert jarvis.messages == []


def test_daemon_consolidates_on_interval_or_backlog():
    now = [T0]
    d, _, _, cons = make_daemon(lambda: now[0], briefing_time="", consolidate_every_min=15, consolidate_after_memos=3)
    d.tick()
    assert cons.runs == 0  # nothing pending
    d.handle(utt("메모 하나"))
    d.tick()
    assert cons.runs == 0  # not yet due
    now[0] = T0 + timedelta(minutes=16)
    d.tick()
    assert cons.runs == 1
    for text in ("가나다라 첫째", "마바사아 둘째", "자차카타 셋째"):
        d.handle(utt(text))
    d.tick()
    assert cons.runs == 2  # backlog trigger


def test_morning_briefing_once_per_day():
    assert briefing_due(datetime(2026, 10, 11, 7, 31), time(7, 30), None)
    assert not briefing_due(datetime(2026, 10, 11, 7, 29), time(7, 30), None)
    assert not briefing_due(datetime(2026, 10, 11, 9, 0), time(7, 30), "2026-10-11")

    now = [datetime(2026, 10, 11, 7, 45)]
    d, jarvis, speaker, cons = make_daemon(lambda: now[0], briefing_time="07:30")
    d.tick()
    d.tick()
    assert len(speaker.said) == 1 and "아침 브리핑" in jarvis.asked[0]
    assert d.store.get_state(LAST_BRIEFING_KEY) == "2026-10-11" and cons.runs == 1
    now[0] = datetime(2026, 10, 12, 7, 30)
    d.tick()
    assert len(speaker.said) == 2


def test_after_the_briefing_only_what_was_said_stays_in_the_conversation():
    # The briefing prompt carried collected titles; voice commands that follow (with every tool)
    # keep the briefing as it was spoken, not the raw material.
    d, jarvis, speaker, _ = make_daemon(lambda: datetime(2026, 10, 11, 7, 45), briefing_time="07:30")
    d.tick()
    assert jarvis.messages == [
        {"role": "user", "content": "(아침 브리핑을 해줘)"},
        {"role": "assistant", "content": speaker.said[0]},
    ]


def test_briefing_is_read_only_but_voice_commands_are_not():
    # The briefing reads collected articles with nobody watching; a command is the owner speaking.
    d, jarvis, _, _ = make_daemon(lambda: datetime(2026, 10, 11, 7, 45), briefing_time="07:30")
    d.tick()
    d.handle(utt("자비스, 목요일 3시 치과 잡아줘"))
    assert jarvis.readonly == [True, False]


def test_failed_briefing_backs_off():
    now = [datetime(2026, 10, 11, 7, 45)]
    d, jarvis, speaker, _ = make_daemon(lambda: now[0], briefing_time="07:30")

    def boom(text, readonly=False):
        jarvis.asked.append(text)
        raise ConnectionError("offline")

    jarvis.ask = boom
    d.tick()
    d.tick()
    assert len(jarvis.asked) == 1  # no retry storm
    now[0] += timedelta(minutes=11)
    d.tick()
    assert len(jarvis.asked) == 2 and d.store.get_state(LAST_BRIEFING_KEY) is None
