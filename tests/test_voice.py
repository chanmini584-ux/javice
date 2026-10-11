"""Voice pipeline tests with synthetic audio and fake models."""

import numpy as np
import pytest

from jarvis.voice import SAMPLE_RATE, Segmenter, VoiceListener, VoicePrint

FRAME = SAMPLE_RATE * 30 // 1000
rng = np.random.default_rng(0)


def noise(seconds, level=0.002):
    return rng.normal(0, level, int(seconds * SAMPLE_RATE)).astype(np.float32)


def tone(seconds, freq=220.0, amp=0.3):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def frames(audio):
    for i in range(0, len(audio), FRAME):
        yield audio[i : i + FRAME]


def test_segmenter_finds_two_utterances_and_ignores_blips():
    audio = np.concatenate(
        [noise(1.0), tone(1.0), noise(1.5), tone(0.1), noise(1.5), tone(2.0, 330), noise(1.5)]
    )
    segs = list(Segmenter().segments(frames(audio)))
    assert len(segs) == 2  # the 0.1 s blip is too short to count
    assert 1.0 <= len(segs[0]) / SAMPLE_RATE <= 2.2
    assert 2.0 <= len(segs[1]) / SAMPLE_RATE <= 3.2


def test_segmenter_splits_overlong_speech():
    segs = list(Segmenter(max_utterance_s=3.0).segments(frames(np.concatenate([noise(1), tone(7), noise(1.5)]))))
    assert len(segs) >= 2 and all(len(s) <= 3.1 * SAMPLE_RATE for s in segs)


class FakeEmbedder:
    """'Speaker identity' is the dominant frequency of the clip."""

    def embed(self, audio):
        spectrum = np.abs(np.fft.rfft(audio[: SAMPLE_RATE]))
        bins = np.zeros(64)
        freqs = np.fft.rfftfreq(SAMPLE_RATE, 1 / SAMPLE_RATE)
        idx = np.clip((freqs[np.argmax(spectrum)] / 20).astype(int), 0, 63)
        bins[idx] = 1.0
        bins[(idx + 1) % 64] = 0.5
        return bins + 0.05


def test_voiceprint_enroll_verify_and_persist(tmp_path):
    path = tmp_path / "vp.npy"
    vp = VoicePrint(FakeEmbedder(), path, threshold=0.8)
    with pytest.raises(ValueError):
        vp.enroll([tone(1.0)])  # too short / too few
    worst = vp.enroll([tone(2.5, 220), tone(3.0, 222), tone(2.2, 224)])
    assert worst > 0.9
    assert vp.verify(tone(2.0, 220))[0]
    assert not vp.verify(tone(2.0, 600))[0]
    assert VoicePrint(FakeEmbedder(), path).enrolled  # reloaded from disk


class ListSource:
    def __init__(self, audio):
        self.audio = audio

    def frames(self):
        return frames(self.audio)


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return "내일 오전에 은행 가야 해"


def test_listener_only_transcribes_owner():
    vp = VoicePrint(FakeEmbedder(), threshold=0.8)
    vp.enroll([tone(2.5, 220)] * 3)
    stt = FakeTranscriber()
    audio = np.concatenate([noise(1), tone(1.5, 220), noise(1.5), tone(1.5, 600), noise(1.5)])
    utts = list(VoiceListener(ListSource(audio), vp, stt).utterances())
    assert [u.is_owner for u in utts] == [True, False]
    assert utts[0].text == "내일 오전에 은행 가야 해" and utts[1].text is None
    assert stt.calls == 1
