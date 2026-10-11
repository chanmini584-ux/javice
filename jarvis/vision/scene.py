"""Turn a noisy stream of per-frame detections into a stable scene and change events."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .base import Detection


@dataclass
class _Track:
    streak: int = 0  # consecutive observations containing the label
    present: bool = False
    since: datetime | None = None
    last_seen: datetime | None = None
    count: int = 0


@dataclass
class SceneEvent:
    kind: str  # "appeared" | "disappeared"
    label: str
    at: datetime
    count: int = 0


@dataclass
class SceneTracker:
    confirm_frames: int = 3  # a label must show up this many times in a row to count
    absent_after_s: float = 10.0  # and be missing this long to count as gone
    ignore: frozenset[str] = frozenset()
    _tracks: dict[str, _Track] = field(default_factory=dict)

    def update(self, detections: list[Detection], now: datetime) -> list[SceneEvent]:
        counts = Counter(d.label for d in detections if d.label not in self.ignore)
        events: list[SceneEvent] = []
        for label in set(counts) | set(self._tracks):
            t = self._tracks.setdefault(label, _Track())
            if counts[label]:
                t.streak += 1
                t.last_seen = now
                t.count = counts[label]
                if not t.present and t.streak >= self.confirm_frames:
                    t.present, t.since = True, now
                    events.append(SceneEvent("appeared", label, now, t.count))
            else:
                t.streak = 0
                if t.present and t.last_seen and (now - t.last_seen).total_seconds() >= self.absent_after_s:
                    t.present = False
                    events.append(SceneEvent("disappeared", label, now))
                    del self._tracks[label]
                elif not t.present:
                    del self._tracks[label]
        return events

    def present(self) -> dict[str, tuple[int, datetime]]:
        """label -> (count, present since)"""
        return {k: (t.count, t.since) for k, t in self._tracks.items() if t.present and t.since}

    def describe(self, now: datetime) -> str:
        items = sorted(self.present().items(), key=lambda kv: kv[1][1])
        if not items:
            return "nothing notable in view"
        parts = []
        for label, (count, since) in items:
            minutes = int((now - since).total_seconds() // 60)
            parts.append(f"{label} x{count} (for {minutes} min)" if count > 1 else f"{label} (for {minutes} min)")
        return ", ".join(parts)
