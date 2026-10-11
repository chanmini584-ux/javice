"""Interfaces for JARVIS's vision: a camera and an object detector.

The detector is pluggable on purpose: plug in the detection model from the CCTV
project (any model that returns labelled boxes) by implementing `Detector`, or use
the built-in Ultralytics YOLO backend with your own trained weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels


class Camera(Protocol):
    def read(self) -> np.ndarray | None:
        """Return the latest frame as an HxWx3 BGR uint8 array, or None if unavailable."""


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Detection]: ...
