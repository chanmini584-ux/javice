"""Concrete microphone, speech-to-text and text-to-speech backends (optional deps)."""

from __future__ import annotations

import queue
import threading
from typing import Iterator

import numpy as np

from .base import SAMPLE_RATE


class Microphone:
    """Default input device via sounddevice, 30 ms frames."""

    def __init__(self, frame_ms: int = 30, device: int | str | None = None) -> None:
        self.blocksize = SAMPLE_RATE * frame_ms // 1000
        self.device = device
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def frames(self) -> Iterator[np.ndarray]:
        import sounddevice as sd

        q: queue.Queue[np.ndarray] = queue.Queue(maxsize=1000)

        def callback(indata, frames, time, status):  # runs on the audio thread
            try:
                q.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass  # drop rather than block the audio thread

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
            device=self.device,
            callback=callback,
        ):
            while not self._stop.is_set():
                try:
                    yield q.get(timeout=0.5)
                except queue.Empty:
                    continue

    def record(self, seconds: float) -> np.ndarray:
        import sounddevice as sd

        audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        return audio[:, 0]


class WhisperTranscriber:
    """Local speech-to-text with faster-whisper (good Korean accuracy, runs offline)."""

    def __init__(self, model_size: str = "small", language: str | None = "ko", device: str = "auto") -> None:
        from faster_whisper import WhisperModel

        self.language = language
        self._model = WhisperModel(model_size, device=device, compute_type="int8")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            np.asarray(audio, dtype=np.float32), language=self.language, beam_size=5
        )
        return " ".join(s.text.strip() for s in segments).strip()


class SystemSpeaker:
    """Offline text-to-speech via pyttsx3 (SAPI5 / NSSpeechSynthesizer / eSpeak)."""

    def __init__(self, rate: int | None = None) -> None:
        import pyttsx3

        self._engine = pyttsx3.init()
        if rate:
            self._engine.setProperty("rate", rate)

    def say(self, text: str) -> None:
        self._engine.say(text)
        self._engine.runAndWait()


class PrintSpeaker:
    """Fallback when no TTS engine is installed."""

    def say(self, text: str) -> None:
        print(f"\n[JARVIS] {text}")
