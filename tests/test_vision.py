"""Vision tests with a fake camera and a scripted detector."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np

from jarvis.agent import Jarvis
from jarvis.config import Settings
from jarvis.daemon import Daemon
from jarvis.memory import MemoryStore
from jarvis.prompts import situation_block
from jarvis.vision import Detection, SceneTracker
from jarvis.vision.eyes import Eyes

T0 = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)


def det(label, conf=0.9):
    return Detection(label, conf, (0.0, 0.0, 10.0, 10.0))


def test_tracker_debounces_appear_and_disappear():
    tr = SceneTracker(confirm_frames=3, absent_after_s=10)
    t = T0
    assert tr.update([det("person")], t) == []
    assert tr.update([], t + timedelta(seconds=1)) == []  # flicker resets the streak
    evs = []
    for i in range(3):
        evs += tr.update([det("person"), det("cup"), det("cup")], t + timedelta(seconds=2 + i))
    assert {(e.kind, e.label) for e in evs} == {("appeared", "person"), ("appeared", "cup")}
    assert tr.present()["cup"][0] == 2
    assert tr.update([det("cup"), det("cup")], t + timedelta(seconds=8)) == []  # person briefly missing
    evs = tr.update([det("cup"), det("cup")], t + timedelta(seconds=15))
    assert [(e.kind, e.label) for e in evs] == [("disappeared", "person")]
    assert "cup x2" in tr.describe(t + timedelta(seconds=15))


class FakeCamera:
    def read(self):
        return np.zeros((4, 4, 3), dtype=np.uint8)


class ScriptedDetector:
    def __init__(self, frames):
        self.frames = list(frames)

    def detect(self, frame):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def make_eyes(script, store=None):
    store = store or MemoryStore()
    return Eyes(FakeCamera(), ScriptedDetector(script), store, SceneTracker(confirm_frames=2), encode=lambda f: "QUJD")


def test_eyes_observe_logs_events_and_feeds_situation():
    eyes = make_eyes([[det("person")]])
    eyes.observe(T0)
    eyes.observe(T0 + timedelta(seconds=2))
    store = eyes.store
    assert store.events()[0]["detail"] == "person"
    block = situation_block(store, now=T0 + timedelta(seconds=3))
    assert "camera view" in block and "person" in block and "vision appeared: person" in block


def test_look_tool_returns_text_or_image_blocks():
    eyes = make_eyes([[det("laptop", 0.81), det("cup", 0.5), det("cup", 0.7)]])
    jarvis = Jarvis(Settings(), store=eyes.store, client=object(), eyes=eyes)
    out, err = jarvis.registry.execute("look", {})
    assert not err and "laptop (conf 0.81)" in out and "cup x2" in out
    blocks, err = jarvis.registry.execute("look", {"send_image": True})
    assert not err and blocks[1]["type"] == "image" and blocks[1]["source"]["data"] == "QUJD"


def test_image_tool_result_reaches_the_model():
    eyes = make_eyes([[det("person")]])
    calls = []

    class Stream:
        def __init__(self, m):
            self.m = m

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return self.m

    script = [
        NS(content=[NS(type="tool_use", id="t1", name="look", input={"send_image": True})], stop_reason="tool_use"),
        NS(content=[NS(type="text", text="책상 앞에 계시네요")], stop_reason="end_turn"),
    ]

    def stream(**params):
        calls.append(list(params["messages"]))
        return Stream(script.pop(0))

    client = NS(beta=NS(messages=NS(stream=stream)))
    jarvis = Jarvis(Settings(), store=eyes.store, client=client, eyes=eyes)
    assert jarvis.ask("나 지금 보여?").text == "책상 앞에 계시네요"
    result = calls[1][-1]["content"][0]
    assert result["content"][1]["type"] == "image"


def test_briefing_waits_until_user_is_in_view():
    store = MemoryStore()
    eyes = make_eyes([[], [], [det("person")]], store)
    now = [datetime(2026, 10, 11, 7, 45)]
    said = []

    class StubJarvis(Jarvis):
        def ask(self, text, readonly=False):
            return NS(text="좋은 아침입니다")

    jarvis = StubJarvis(Settings(briefing_time="07:30", vision_every_s=0), store=store, client=object(), eyes=eyes)
    d = Daemon(
        jarvis,
        None,
        NS(say=said.append),
        NS(run=lambda now=None: NS(summary=lambda: "ok")),
        clock=lambda: now[0],
        log=lambda s: None,
    )
    d.tick()
    d.tick()
    assert said == []  # nobody in front of the camera yet
    d.tick()
    d.tick()  # person confirmed on the second sighting
    assert said == ["좋은 아침입니다"]
