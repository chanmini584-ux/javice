"""Always-on mode: listen, memo, consolidate, brief.

The listener runs on a background thread and hands utterances over a queue; all
memory and model work happens on the main thread (SQLite connections are
single-threaded).
"""

from __future__ import annotations

import queue
import re
import threading
from datetime import datetime, timedelta
from typing import Callable

from .agent import Jarvis
from .briefing import briefing_due, briefing_prompt, parse_time
from .consolidate import Consolidator
from .voice.base import Speaker, Utterance
from .voice.listener import VoiceListener

WAKE_WORD = re.compile(r"^\s*(?:헤이\s*)?(?:자비스|자빗|jarvis)[\s,.!?~]*", re.IGNORECASE)
LAST_BRIEFING_KEY = "last_briefing_date"


class Daemon:
    def __init__(
        self,
        jarvis: Jarvis,
        listener: VoiceListener | None,
        speaker: Speaker,
        consolidator: Consolidator,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        log: Callable[[str], None] = print,
    ) -> None:
        self.jarvis = jarvis
        self.store = jarvis.store
        self.listener = listener
        self.speaker = speaker
        self.consolidator = consolidator
        self.clock = clock
        self.log = log
        s = jarvis.settings
        self.briefing_at = parse_time(s.briefing_time) if s.briefing_time else None
        self.consolidate_every = timedelta(minutes=s.consolidate_every_min)
        self.consolidate_after = s.consolidate_after_memos
        self.last_consolidation = self.clock()
        self.others_heard = 0
        self._brief_retry_at: datetime | None = None
        self._queue: queue.Queue[Utterance] = queue.Queue()
        self._stop = threading.Event()

    # -- events ------------------------------------------------------------------

    def handle(self, utt: Utterance) -> None:
        if not utt.is_owner:
            self.others_heard += 1  # someone else: not transcribed, not stored
            return
        if not utt.text:
            return
        match = WAKE_WORD.match(utt.text)
        if match:
            command = utt.text[match.end() :].strip()
            if command:
                self.log(f"command: {command}")
                result = self.jarvis.ask(f"(음성 명령, 짧게 말로 답해줘) {command}")
                self.speaker.say(result.text)
            return
        memo_id, is_new = self.store.add_memo(utt.text)
        self.log(f"memo #{memo_id} {'new' if is_new else 're-mentioned'}: {utt.text}")

    def tick(self) -> None:
        now = self.clock()
        pending = len(self.store.memos(status="pending", limit=self.consolidate_after))
        if pending and (pending >= self.consolidate_after or now - self.last_consolidation >= self.consolidate_every):
            self.consolidate(now)
        if (
            self.briefing_at
            and briefing_due(now, self.briefing_at, self.store.get_state(LAST_BRIEFING_KEY))
            and (self._brief_retry_at is None or now >= self._brief_retry_at)
        ):
            try:
                self.brief(now)
            except Exception as exc:  # e.g. network down: try again later, not every second
                self._brief_retry_at = now + timedelta(minutes=10)
                self.log(f"briefing failed, retrying in 10 min: {exc}")

    def consolidate(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        try:
            report = self.consolidator.run(now)
            self.log(f"consolidated: {report.summary()}")
        except Exception as exc:  # keep listening; retry on the next interval
            self.log(f"consolidation failed: {exc}")
        self.last_consolidation = now

    def brief(self, now: datetime | None = None) -> str:
        now = now or self.clock()
        self.consolidate(now)  # fold in last night's memos first
        self.jarvis.reset()  # a new day starts a new conversation; long-term memory stays
        text = self.jarvis.ask(briefing_prompt(self.store, now)).text
        self.store.set_state(LAST_BRIEFING_KEY, now.date().isoformat())
        self.speaker.say(text)
        return text

    # -- run loop ----------------------------------------------------------------

    def _listen(self) -> None:
        assert self.listener is not None
        try:
            for utt in self.listener.utterances():
                if self._stop.is_set():
                    break
                self._queue.put(utt)
        except Exception as exc:
            self.log(f"listener stopped: {exc}")
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self.listener:
            threading.Thread(target=self._listen, name="jarvis-listener", daemon=True).start()
        self.log("JARVIS is listening. Say '자비스, ...' to give a command. Ctrl+C to stop.")
        while not self._stop.is_set():
            try:
                self.handle(self._queue.get(timeout=1.0))
            except queue.Empty:
                pass
            except Exception as exc:  # one bad command must not stop the listener
                self.log(f"command failed: {exc}")
            self.tick()
