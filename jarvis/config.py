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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

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
        )
