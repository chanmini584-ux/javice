"""Mic frames -> utterances -> owner check -> transcript."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from .base import AudioSource, Transcriber, Utterance
from .speaker_id import VoicePrint
from .vad import Segmenter


class VoiceListener:
    """Yields every detected utterance; only the owner's speech is transcribed.

    Other people's speech is detected and counted but never transcribed or stored,
    which keeps bystanders out of your memory by design.
    """

    def __init__(
        self,
        source: AudioSource,
        voiceprint: VoicePrint,
        transcriber: Transcriber,
        segmenter: Segmenter | None = None,
    ) -> None:
        self.source = source
        self.voiceprint = voiceprint
        self.transcriber = transcriber
        self.segmenter = segmenter or Segmenter()

    def utterances(self) -> Iterator[Utterance]:
        for audio in self.segmenter.segments(self.source.frames()):
            started = datetime.now(timezone.utc)
            is_owner, score = self.voiceprint.verify(audio)
            utt = Utterance(audio=audio, started_at=started, is_owner=is_owner, owner_score=score)
            if is_owner:
                utt.text = self.transcriber.transcribe(audio) or None
            yield utt
