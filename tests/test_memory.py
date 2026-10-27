import pytest

from jarvis.memory import MemoryStore


@pytest.fixture
def store():
    s = MemoryStore()
    yield s
    s.close()


def test_profile_upsert(store):
    store.set_profile("name", "Chan")
    store.set_profile("name", "Chanmin")
    assert store.profile() == {"name": "Chanmin"}


def test_recall_ranks_by_term_hits_then_importance(store):
    store.remember("fact", "Python과 Java를 주로 사용한다", 3)
    store.remember("weakness", "영어 문서 작성이 약하다", 5)
    store.remember("preference", "Python 타입 힌트를 선호한다", 4)
    hits = store.recall("Python 타입")
    assert [m.content for m in hits][0] == "Python 타입 힌트를 선호한다"
    assert len(hits) == 2
    # Korean particles attached to words still match by substring.
    assert store.recall("영어")[0].kind == "weakness"


def test_recall_filters_by_kind_and_empty_query(store):
    store.remember("weakness", "a", 1)
    store.remember("weakness", "b", 5)
    store.remember("fact", "c", 5)
    assert [m.content for m in store.recall(kind="weakness")] == ["b", "a"]


def test_invalid_kind_rejected(store):
    with pytest.raises(ValueError):
        store.remember("gossip", "x")


def test_goals_and_plans(store):
    gid = store.add_goal("Ship JARVIS MVP", "personal assistant", "2026-12-31")
    ids = store.save_plan(gid, ["design", "build", "test"])
    assert [s.description for s in store.plan(gid)] == ["design", "build", "test"]
    assert store.set_step_status(ids[0], "done")
    store.save_plan(gid, ["only step"])  # replaces the old plan
    assert len(store.plan(gid)) == 1
    store.set_goal_status(gid, "done")
    assert store.goals("active") == []
    with pytest.raises(ValueError):
        store.save_plan(999, ["x"])


def test_goal_deadline_must_be_a_date(store):
    # Deadlines are compared and sorted as text; "다음 주" would sort after every real date.
    with pytest.raises(ValueError):
        store.add_goal("발표 준비", "학점", "다음 주")
    gid = store.add_goal("발표 준비", "학점", "2026-10-31T18:00")
    assert store.goals()[0].id == gid and store.goals()[0].deadline == "2026-10-31"
