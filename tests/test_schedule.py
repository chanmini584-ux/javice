"""JARVIS's own schedule: storage, tools, situation/briefing, voice and web reminders."""

from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from jarvis.agent import Jarvis
from jarvis.briefing import briefing_prompt
from jarvis.config import Settings
from jarvis.daemon import Daemon
from jarvis.memory import MemoryStore, parse_when
from jarvis.prompts import situation_block

NOW = datetime(2026, 10, 13, 14, 0)  # Tuesday


def test_parse_when_normalizes_and_rejects():
    assert parse_when("2026-10-15") == ("2026-10-15", True)
    assert parse_when("2026-10-15T9:05:00+09:00") == ("2026-10-15 09:05", False)
    with pytest.raises(ValueError):
        parse_when("다음 주")
    with pytest.raises(ValueError):
        parse_when("2026-02-30 10:00")


def test_store_range_update_and_reminder_window():
    s = MemoryStore()
    dentist = s.add_schedule("치과", "2026-10-15 15:00", remind_min=30)
    trip = s.add_schedule("여행", "2026-10-16")
    s.add_schedule("지난 일", "2026-10-12 10:00")
    assert [i.title for i in s.schedule(NOW, days=7)] == ["치과", "여행"]
    assert s.get_schedule(trip).remind_min == 0  # all-day items have no timed reminder

    assert s.reminders_due(datetime(2026, 10, 15, 14, 29)) == []
    assert [i.id for i in s.reminders_due(datetime(2026, 10, 15, 14, 30))] == [dentist]
    assert [i.id for i in s.reminders_due(datetime(2026, 10, 15, 15, 10))] == [dentist]  # within grace
    assert s.reminders_due(datetime(2026, 10, 15, 15, 11)) == []

    s.mark_reminded(dentist)
    assert s.reminders_due(datetime(2026, 10, 15, 14, 40)) == []
    assert s.reminders_due(datetime(2026, 10, 15, 14, 40), include_reminded=True)
    s.update_schedule(dentist, start="2026-10-15 16:00")  # moving it re-arms the reminder
    assert [i.id for i in s.reminders_due(datetime(2026, 10, 15, 15, 40))] == [dentist]

    s.update_schedule(dentist, status="cancelled")
    assert [i.title for i in s.schedule(NOW, days=7)] == ["여행"]
    assert len(s.schedule(NOW, days=7, include_closed=True)) == 2
    with pytest.raises(ValueError):
        s.update_schedule(trip, status="nope")
    assert s.update_schedule(999, title="x") is False


@pytest.mark.parametrize("now", [datetime(2026, 10, 14, 9, 0), datetime(2026, 10, 19, 0, 5)])
def test_reminder_set_days_ahead_fires_when_its_window_opens(now):
    # The web form allows reminders up to 7 days ahead; they must not wait until the last 2 days.
    s = MemoryStore()
    s.add_schedule("기말고사", "2026-10-21 09:00", remind_min=7 * 24 * 60)
    s.add_schedule("발표", "2026-10-20 23:59", remind_min=2 * 24 * 60 - 1)  # opens 10/19 00:00
    due = {i.title for i in s.reminders_due(now)}
    assert "기말고사" in due and ("발표" in due) == (now.day == 19)
    assert s.reminders_due(datetime(2026, 10, 14, 8, 59)) == []


def test_tools_add_list_update():
    jarvis = Jarvis(Settings(), store=MemoryStore(), client=object())
    run = jarvis.registry.execute
    out, err = run("add_schedule", {"title": "치과", "start": "2026-10-15 15:00", "location": "강남"})
    assert not err and "Thursday" in out and "강남" in out
    out, err = run("add_schedule", {"title": "회의", "start": "2026-10-15T15:00"})
    assert "same start time as: #1" in out
    out, err = run("add_schedule", {"title": "?", "start": "목요일"})
    assert err and "YYYY-MM-DD" in out
    out, _ = run("list_schedule", {"from_date": "2026-10-15", "days": 1})
    assert out.splitlines()[0].startswith("#1 2026-10-15 15:00 치과")
    out, err = run("update_schedule", {"id": 2, "status": "cancelled"})
    assert not err and "[cancelled]" in out
    out, err = run("update_schedule", {"id": 1, "remind_minutes": 60, "start": "2026-10-15 16:00"})
    assert not err and jarvis.store.get_schedule(1).remind_min == 60
    out, err = run("update_schedule", {"id": 42, "title": "x"})
    assert err


def test_schedule_reaches_situation_and_briefing():
    s = MemoryStore()
    s.add_schedule("팀 회의", "2026-10-13 16:00")
    s.add_schedule("치과", "2026-10-14 09:30")
    s.add_schedule("다음 주", "2026-10-20 09:00")
    block = situation_block(s, NOW)
    assert "today #1 2026-10-13 16:00 팀 회의" in block and "tomorrow #2" in block and "다음 주" not in block
    prompt = briefing_prompt(s, NOW)
    assert "#1 2026-10-13 16:00 팀 회의" in prompt and "치과" not in prompt.split("</schedule_today>")[0]


def test_daemon_speaks_each_reminder_once():
    now = [datetime(2026, 10, 13, 15, 49)]
    store = MemoryStore()
    store.add_schedule("팀 회의", "2026-10-13 16:00", location="3층")
    said = []
    jarvis = Jarvis(Settings(briefing_time="", collect_time=""), store=store, client=object())
    d = Daemon(jarvis, None, NS(say=said.append), NS(run=lambda now=None: None), clock=lambda: now[0], log=lambda s: None)
    d.tick()
    assert said == []
    now[0] = datetime(2026, 10, 13, 15, 50)
    d.tick()
    d.tick()
    assert said == ["10분 뒤 팀 회의 일정이 있습니다, 장소는 3층."]
    assert store.events(limit=1)[0]["kind"] == "reminder"
