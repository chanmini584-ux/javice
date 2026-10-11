"""Energy-based voice activity detection that cuts a frame stream into utterances.

It tracks an adaptive noise floor, so it copes with a quiet room and a noisy café
alike. Swap in Silero VAD later if false triggers become a problem.
"""

from __future__ import annotations

from typing import Iterable, Iterator

import numpy as np

from .base import SAMPLE_RATE


class Segmenter:
    def __init__(
        self,
        threshold_db: float = 10.0,  # how far above the noise floor counts as speech
        min_speech_s: float = 0.4,
        max_silence_s: float = 0.8,  # pause that ends an utterance
        max_utterance_s: float = 30.0,
        pre_roll_s: float = 0.2,  # keep a little audio before speech onset
        min_floor_db: float = -60.0,
        calibrate_s: float = 0.5,  # treat the first moments as room noise
    ) -> None:
        self.threshold_db = threshold_db
        self.min_speech = int(min_speech_s * SAMPLE_RATE)
        self.max_silence = int(max_silence_s * SAMPLE_RATE)
        self.max_utterance = int(max_utterance_s * SAMPLE_RATE)
        self.pre_roll = int(pre_roll_s * SAMPLE_RATE)
        self.floor_db = min_floor_db
        self.min_floor_db = min_floor_db
        self.calibrate = int(calibrate_s * SAMPLE_RATE)

    @staticmethod
    def level_db(frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) if len(frame) else 0.0
        return 20 * np.log10(max(rms, 1e-10))

    def segments(self, frames: Iterable[np.ndarray]) -> Iterator[np.ndarray]:
        buf: list[np.ndarray] = []
        pre: list[np.ndarray] = []
        pre_len = speech_len = silence_len = 0
        active = False
        calib: list[float] = []
        seen = 0
        for frame in frames:
            db = self.level_db(frame)
            if seen < self.calibrate:
                seen += len(frame)
                calib.append(db)
                self.floor_db = max(self.min_floor_db, float(np.median(calib)))
                continue
            is_speech = db > self.floor_db + self.threshold_db
            # Follow the noise floor quickly down and slowly up; the tiny drift during
            # "speech" lets it recover if steady background noise got louder.
            rate = 0.3 if db < self.floor_db else (0.02 if not is_speech else 0.0005)
            self.floor_db = max(self.min_floor_db, self.floor_db + rate * (db - self.floor_db))
            if not active:
                if is_speech:
                    active, buf = True, [*pre, frame]
                    speech_len, silence_len = len(frame), 0
                    pre, pre_len = [], 0
                else:
                    pre.append(frame)
                    pre_len += len(frame)
                    while pre and pre_len - len(pre[0]) >= self.pre_roll:
                        pre_len -= len(pre.pop(0))
                continue
            buf.append(frame)
            if is_speech:
                speech_len += len(frame)
                silence_len = 0
            else:
                silence_len += len(frame)
            total = sum(len(b) for b in buf)
            if silence_len >= self.max_silence or total >= self.max_utterance:
                if speech_len >= self.min_speech:
                    yield np.concatenate(buf)
                active, buf = False, []
        if active and speech_len >= self.min_speech:
            yield np.concatenate(buf)
