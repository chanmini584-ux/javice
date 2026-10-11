"""JARVIS's eyes: camera + detector + scene tracking, exposed to the agent and the daemon.

Frames never leave the machine on their own. Only detection labels go into the
situation block; an actual image is sent to Claude only when the agent calls
`look` with `send_image` because it needs to see details the detector can't name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import numpy as np

from ..memory import MemoryStore
from ..tools import ToolError, ToolOutput, ToolRegistry
from .base import Camera, Detection, Detector
from .scene import SceneEvent, SceneTracker

SCENE_STATE_KEY = "vision_scene"


def _summarize(detections: list[Detection]) -> str:
    if not detections:
        return "no objects detected"
    best: dict[str, list[float]] = {}
    for d in detections:
        best.setdefault(d.label, []).append(d.confidence)
    return ", ".join(
        f"{label} x{len(c)} (conf {max(c):.2f})" if len(c) > 1 else f"{label} (conf {c[0]:.2f})"
        for label, c in sorted(best.items(), key=lambda kv: -max(kv[1]))
    )


class Eyes:
    def __init__(
        self,
        camera: Camera,
        detector: Detector,
        store: MemoryStore,
        tracker: SceneTracker | None = None,
        encode: Callable[[np.ndarray], str] | None = None,
    ) -> None:
        self.camera = camera
        self.detector = detector
        self.store = store
        self.tracker = tracker or SceneTracker()
        self._encode = encode

    def snapshot(self) -> tuple[np.ndarray, list[Detection]]:
        frame = self.camera.read()
        if frame is None:
            raise ToolError("camera returned no frame")
        return frame, self.detector.detect(frame)

    def observe(self, now: datetime | None = None) -> list[SceneEvent]:
        """One tick of continuous watching: detect, update the scene, log changes."""
        now = now or datetime.now(timezone.utc)
        _frame, detections = self.snapshot()
        events = self.tracker.update(detections, now)
        for e in events:
            detail = f"{e.label} x{e.count}" if e.kind == "appeared" and e.count > 1 else e.label
            self.store.add_event("vision", e.kind, detail, now)
        self.store.set_state(SCENE_STATE_KEY, f"{now.isoformat(timespec='seconds')}|{self.tracker.describe(now)}")
        return events

    def look(self, send_image: bool = False) -> str | ToolOutput:
        frame, detections = self.snapshot()
        text = f"detector sees: {_summarize(detections)}"
        if not send_image:
            return text
        if self._encode is None:
            from .backends import encode_jpeg

            self._encode = encode_jpeg
        return ToolOutput(text + "\n(camera frame attached)", [self._encode(frame)])

    def register_tools(self, registry: ToolRegistry) -> None:
        @registry.tool(
            "look",
            "Look through the user's camera right now. Returns what the object detector sees. "
            "Set send_image=true to also receive the frame itself when you need details the "
            "detector cannot name (text, expressions, what someone is doing). Prefer false.",
            {"send_image": {"type": "boolean"}},
            required=[],
        )
        def look(send_image: bool = False):
            return self.look(send_image)
