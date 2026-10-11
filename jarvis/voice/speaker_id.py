"""Speaker verification: is this *my* voice or someone else's?

Enrollment averages embeddings from a few samples of the owner's voice into a
voiceprint. Verification compares an utterance's embedding against it by cosine
similarity. The default embedder is SpeechBrain's ECAPA-TDNN (VoxCeleb), where
same-speaker cosine scores usually sit well above 0.5 and different speakers below it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np

from .base import SAMPLE_RATE, Embedder


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n else v


class VoicePrint:
    def __init__(self, embedder: Embedder, path: Path | None = None, threshold: float = 0.5) -> None:
        self.embedder = embedder
        self.path = path
        self.threshold = threshold
        self.vector: np.ndarray | None = None
        if path and path.exists():
            self.vector = np.load(path)

    @property
    def enrolled(self) -> bool:
        return self.vector is not None

    def enroll(self, samples: Sequence[np.ndarray], min_seconds: float = 2.0) -> float:
        """Build the voiceprint. Returns the worst sample-to-print score as a quality check."""
        usable = [s for s in samples if len(s) >= min_seconds * SAMPLE_RATE]
        if len(usable) < 3:
            raise ValueError(f"need at least 3 samples of {min_seconds}s or longer")
        embs = [_unit(self.embedder.embed(s)) for s in usable]
        self.vector = _unit(np.mean(embs, axis=0))
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            np.save(self.path, self.vector)
        return min(float(e @ self.vector) for e in embs)

    def score(self, audio: np.ndarray) -> float:
        if self.vector is None:
            raise RuntimeError("no voiceprint enrolled; run `jarvis enroll` first")
        return float(_unit(self.embedder.embed(audio)) @ self.vector)

    def verify(self, audio: np.ndarray) -> tuple[bool, float]:
        s = self.score(audio)
        return s >= self.threshold, s


class EcapaEmbedder:
    """SpeechBrain ECAPA-TDNN speaker embeddings (192-d). Requires `pip install jarvis[voice]`."""

    def __init__(self, cache_dir: Path | None = None, device: str = "cpu") -> None:
        import torch
        from speechbrain.inference.classifiers import EncoderClassifier

        self._torch = torch
        self._model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(cache_dir) if cache_dir else None,
            run_opts={"device": device},
        )

    def embed(self, audio: np.ndarray) -> np.ndarray:
        wav = self._torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)
        with self._torch.no_grad():
            return self._model.encode_batch(wav).squeeze().cpu().numpy()
