"""Interfaces for the voice pipeline. Real backends live in sibling modules and
import their heavy dependencies lazily, so the core works (and tests run) without them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol

import numpy as np

SAMPLE_RATE = 16_000  # every component works on 16 kHz mono float32 in [-1, 1]


@dataclass
class Utterance:
    audio: np.ndarray
    started_at: datetime
    is_owner: bool
    owner_score: float
    text: str | None = None  # only transcribed when it is the owner speaking

    @property
    def seconds(self) -> float:
        return len(self.audio) / SAMPLE_RATE


class AudioSource(Protocol):
    def frames(self) -> Iterator[np.ndarray]:
        """Yield consecutive mono float32 frames at SAMPLE_RATE until stopped."""


class Embedder(Protocol):
    def embed(self, audio: np.ndarray) -> np.ndarray:
        """Return a fixed-size speaker embedding for an utterance."""


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


class Speaker(Protocol):
    def say(self, text: str) -> None: ...
