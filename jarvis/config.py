"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    # Opus 5.5 defaults to "medium"; planning/judgment work benefits from more depth.
    effort: str = "high"
    max_tokens: int = 64000
    max_steps: int = 25  # upper bound on model<->tool round trips per user turn
    data_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis")
    enable_web_search: bool = True
    # voice / daemon
    briefing_time: str = "07:30"  # local HH:MM; empty disables the morning briefing
    consolidate_every_min: int = 15
    consolidate_after_memos: int = 20  # also consolidate early once this many are pending
    owner_threshold: float = 0.5  # cosine score needed to accept a voice as yours
    whisper_model: str = "small"
    language: str = "ko"
    # vision: camera index ("0") or stream URL; empty disables the camera
    camera: str = ""
    detector_weights: str = "yolo11n.pt"  # or your own trained weights, e.g. from the CCTV project
    vision_every_s: float = 2.0
    # knowledge: daily feed collection
    collect_time: str = "06:30"  # local HH:MM; empty disables daily collection
    knowledge_keep_days: int = 90
    # proactive thinking: local HH:MM times, comma-separated; empty disables it
    reflect_times: str = "12:30,18:00,21:30"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def knowledge_path(self) -> Path:
        return self.data_dir / "knowledge.db"

    @property
    def sources_path(self) -> Path:
        return self.data_dir / "sources.json"

    @property
    def voiceprint_path(self) -> Path:
        return self.data_dir / "voiceprint.npy"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            model=env.get("JARVIS_MODEL", cls.model),
            effort=env.get("JARVIS_EFFORT", cls.effort),
            max_tokens=int(env.get("JARVIS_MAX_TOKENS", cls.max_tokens)),
            max_steps=int(env.get("JARVIS_MAX_STEPS", cls.max_steps)),
            data_dir=Path(env.get("JARVIS_HOME", Path.home() / ".jarvis")),
            enable_web_search=env.get("JARVIS_WEB_SEARCH", "1") != "0",
            briefing_time=env.get("JARVIS_BRIEFING_TIME", cls.briefing_time),
            consolidate_every_min=int(env.get("JARVIS_CONSOLIDATE_MIN", cls.consolidate_every_min)),
            consolidate_after_memos=int(env.get("JARVIS_CONSOLIDATE_AFTER", cls.consolidate_after_memos)),
            owner_threshold=float(env.get("JARVIS_OWNER_THRESHOLD", cls.owner_threshold)),
            whisper_model=env.get("JARVIS_WHISPER_MODEL", cls.whisper_model),
            language=env.get("JARVIS_LANGUAGE", cls.language),
            camera=env.get("JARVIS_CAMERA", cls.camera),
            detector_weights=env.get("JARVIS_DETECTOR_WEIGHTS", cls.detector_weights),
            vision_every_s=float(env.get("JARVIS_VISION_EVERY_S", cls.vision_every_s)),
            collect_time=env.get("JARVIS_COLLECT_TIME", cls.collect_time),
            knowledge_keep_days=int(env.get("JARVIS_KNOWLEDGE_KEEP_DAYS", cls.knowledge_keep_days)),
            reflect_times=env.get("JARVIS_REFLECT_TIMES", cls.reflect_times),
        )
