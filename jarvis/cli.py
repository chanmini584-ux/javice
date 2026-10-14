"""Interactive terminal front end: `python -m jarvis` or `jarvis`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .agent import Jarvis
from .config import Settings

HELP = """\
commands:
  /profile            show your stored profile
  /memories [query]   search long-term memory
  /goals              show active goals and plans
  /memos              show recent voice memos
  /new                start a fresh conversation (memory is kept)
  /think              toggle showing Claude's reasoning summary
  /quit               exit
"""


def _approve(name: str, tool_input: dict[str, Any]) -> bool:
    print(f"\n[approval] JARVIS wants to run `{name}`:")
    for key, value in tool_input.items():
        print(f"  {key}:\n" + "\n".join("    " + line for line in str(value).splitlines()))
    return input("allow? [y/N] ").strip().lower() in ("y", "yes")


ENROLL_SENTENCES = [
    "안녕 자비스, 지금부터 내 목소리를 기억해 줘.",
    "오늘 해야 할 일을 정리하고 우선순위를 알려줘.",
    "다음 주까지 프로젝트 계획을 세워야 해.",
    "점심 먹고 나서 운동하러 갈 거야.",
    "이 숫자들을 계산해서 평균을 알려줄래?",
]


def _make_speaker():
    from .voice.backends import PrintSpeaker, SystemSpeaker

    try:
        return SystemSpeaker()
    except Exception:  # no TTS engine installed
        return PrintSpeaker()


def _make_voiceprint(settings: Settings):
    from .voice.speaker_id import EcapaEmbedder, VoicePrint

    embedder = EcapaEmbedder(cache_dir=settings.data_dir / "models" / "ecapa")
    return VoicePrint(embedder, settings.voiceprint_path, settings.owner_threshold)


def _make_eyes(settings: Settings, store):
    if not settings.camera:
        return None
    from .vision.backends import OpenCVCamera, YoloDetector
    from .vision.eyes import Eyes

    source = int(settings.camera) if settings.camera.isdigit() else settings.camera
    return Eyes(OpenCVCamera(source), YoloDetector(settings.detector_weights), store)


def _make_jarvis(settings: Settings, **kwargs) -> Jarvis:
    from .knowledge import KnowledgeBase
    from .memory import MemoryStore

    store = MemoryStore(settings.db_path)
    kb = KnowledgeBase(settings.knowledge_path)
    return Jarvis(settings, store=store, eyes=_make_eyes(settings, store), knowledge=kb, **kwargs)


def _collect_job(settings: Settings, log=print):
    """Build the daily collection job. It opens its own DB connection (it runs on another thread)."""

    def job() -> str:
        from .knowledge import Collector, KnowledgeBase, load_sources

        kb = KnowledgeBase(settings.knowledge_path)
        try:
            report = Collector(kb).collect(load_sources(settings.sources_path), log=log)
            pruned = kb.prune(settings.knowledge_keep_days)
            return f"{report.summary()}, pruned {pruned}, total {kb.count()}"
        finally:
            kb.close()

    return job


def cmd_enroll(settings: Settings) -> int:
    from .voice.backends import Microphone

    mic, vp = Microphone(), _make_voiceprint(settings)
    print("목소리 등록: 문장이 보이면 Enter를 누르고 4초 동안 자연스럽게 읽어주세요.")
    samples = []
    for i, sentence in enumerate(ENROLL_SENTENCES, start=1):
        input(f"\n[{i}/{len(ENROLL_SENTENCES)}] \"{sentence}\"  (Enter로 녹음 시작)")
        samples.append(mic.record(4.0))
    worst = vp.enroll(samples)
    print(f"\n등록 완료: {settings.voiceprint_path}")
    print(f"가장 낮은 샘플 일치도 {worst:.2f} (임계값 {settings.owner_threshold:.2f})")
    if worst < settings.owner_threshold + 0.1:
        print("일치도가 낮습니다. 조용한 곳에서 다시 등록하는 걸 권합니다.")
    return 0


def _make_daemon(settings: Settings, with_voice: bool):
    from .consolidate import Consolidator
    from .daemon import Daemon

    jarvis = _make_jarvis(settings)  # approvals default to "deny" when nobody is at the keyboard
    listener = None
    if with_voice:
        from .voice.backends import Microphone, WhisperTranscriber
        from .voice.listener import VoiceListener

        vp = _make_voiceprint(settings)
        if not vp.enrolled:
            raise SystemExit("등록된 목소리가 없습니다. 먼저 `jarvis enroll`을 실행하세요.")
        listener = VoiceListener(Microphone(), vp, WhisperTranscriber(settings.whisper_model, settings.language))
    from .reflect import Reflector

    return Daemon(
        jarvis,
        listener,
        _make_speaker(),
        Consolidator(jarvis.client, jarvis.store, settings),
        collect_job=_collect_job(settings),
        reflector=Reflector(jarvis.client, jarvis.store, settings, jarvis.knowledge),
    )


def _ensure_api_key(settings: Settings) -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    import getpass

    key = getpass.getpass("Anthropic API 키 (입력 내용은 보이지 않습니다): ").strip()
    if not key:
        raise SystemExit("API 키가 필요합니다.")
    os.environ["ANTHROPIC_API_KEY"] = key
    path = settings.data_dir / SECRETS_FILE
    answer = input(f"{path}에 저장할까요? USB를 잃어버리면 키가 노출될 수 있습니다. [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _lan_addresses() -> list[str]:
    import socket

    addrs = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the LAN interface
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def _run_daemon(settings: Settings, with_voice: bool) -> None:
    """Thread target for `serve --listen`. A thread ends silently on SystemExit (e.g. no voice
    enrolled), so always say why always-on mode stopped; the web server then takes over."""
    try:
        _make_daemon(settings, with_voice=with_voice).run()
        reason = "listener stopped"
    except BaseException as exc:  # the thread ends either way; make the reason visible
        reason = str(exc) or type(exc).__name__
    print(f"\n[상시 모드 중지] {reason}\n스스로 챙기기(능동 사고)는 웹 서버가 이어서 합니다.", file=sys.stderr)


def cmd_serve(settings: Settings, argv: list[str]) -> int:
    import threading

    import uvicorn

    from .web.server import create_app, load_or_create_token

    parser = argparse.ArgumentParser(prog="jarvis serve")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to allow phones on the same network")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--listen", action="store_true", help="also run the always-on voice mode")
    parser.add_argument("--no-voice", action="store_true", help="with --listen: briefing/collection only")
    parser.add_argument("--open", action="store_true", help="open the chat in the default browser")
    args = parser.parse_args(argv)

    _ensure_api_key(settings)
    token = load_or_create_token(settings.data_dir / "web_token")
    daemon = None
    if args.listen:
        daemon = threading.Thread(
            target=_run_daemon, args=(settings, not args.no_voice), name="jarvis-daemon", daemon=True
        )
        daemon.start()
    # The daemon runs the scheduled reflections; whenever it is not running, the web server does.
    app = create_app(
        lambda: _make_jarvis(settings), token, proactive=lambda: daemon is None or not daemon.is_alive()
    )

    hosts = ["127.0.0.1"] + (_lan_addresses() if args.host == "0.0.0.0" else [])
    print("\nJARVIS 웹 앱 주소 (처음 한 번 이 주소로 열면 토큰이 저장됩니다):")
    for h in hosts:
        print(f"  http://{h}:{args.port}/#token={token}")
    if args.host == "0.0.0.0":
        print("휴대폰에서 앱으로 설치하고 마이크를 쓰려면 HTTPS가 필요합니다. README의 'Tailscale' 항목을 참고하세요.")
    if args.open:
        import webbrowser

        url = f"http://127.0.0.1:{args.port}/#token={token}"
        threading.Timer(1.5, webbrowser.open, [url]).start()  # after uvicorn starts listening
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_listen(settings: Settings, with_voice: bool) -> int:
    daemon = _make_daemon(settings, with_voice)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stop()
        print("\nstopping; consolidating pending memos...")
        daemon.consolidate()
    return 0


SECRETS_FILE = "secrets.env"


def _load_secrets(settings: Settings) -> None:
    """Load KEY=VALUE lines from <JARVIS_HOME>/secrets.env without overriding the environment."""
    path = settings.data_dir / SECRETS_FILE
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    settings = Settings.from_env()
    _load_secrets(settings)
    if argv and argv[0] == "serve":
        return cmd_serve(settings, argv[1:])
    if argv and argv[0] == "enroll":
        return cmd_enroll(settings)
    if argv and argv[0] == "listen":
        return cmd_listen(settings, with_voice="--no-voice" not in argv)
    if argv and argv[0] == "reflect":
        from .reflect import Reflector

        jarvis = _make_jarvis(settings)
        notices = Reflector(jarvis.client, jarvis.store, settings, jarvis.knowledge).run()
        for n in notices:
            print(f"[{n['priority']}] {n['title']}: {n['message']}")
        print(f"{len(notices)} notice(s)" if notices else "지금은 따로 알릴 것이 없습니다.")
        return 0
    if argv and argv[0] == "collect":
        print(_collect_job(settings)())
        return 0
    if argv and argv[0] in ("brief", "consolidate"):
        from .consolidate import Consolidator
        from .daemon import Daemon

        jarvis = _make_jarvis(settings)
        daemon = Daemon(jarvis, None, _make_speaker(), Consolidator(jarvis.client, jarvis.store, settings))
        if argv[0] == "brief":
            daemon.brief()
        else:
            daemon.consolidate()
        return 0

    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Personal AI agent on Claude.",
        epilog="subcommands: enroll (register your voice), listen [--no-voice] (always-on mode), "
        "brief (morning briefing now), consolidate (judge pending voice memos now), "
        "collect (fetch today's articles now; sources in ~/.jarvis/sources.json), "
        "reflect (let JARVIS review your situation now and report what matters), "
        "serve [--host 0.0.0.0] [--listen] (web app for any browser or phone)",
    )
    parser.add_argument("prompt", nargs="*", help="one-shot prompt; omit for interactive mode")
    args = parser.parse_args(argv)

    show_thinking = {"on": False}
    streamed = {"any": False}

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "text":  # the answer types out as it is generated
            if not streamed["any"]:
                print()
            streamed["any"] = True
            print(data["text"], end="", flush=True)
        elif kind == "thinking" and show_thinking["on"]:
            print(f"  (thinking) {data['text']}", file=sys.stderr)
        elif kind == "tool":
            mark = "!" if data["is_error"] else "·"
            print(f"  {mark} {data['name']}({json.dumps(data['input'], ensure_ascii=False)[:120]})", file=sys.stderr)
        elif kind == "server_tool":
            print(f"  · {data['name']}({json.dumps(data['input'], ensure_ascii=False)[:120]})", file=sys.stderr)

    jarvis = _make_jarvis(settings, approve=_approve, on_event=on_event)

    if args.prompt:
        result = jarvis.ask(" ".join(args.prompt))
        print() if streamed["any"] else print(result.text)
        return 0

    print("JARVIS online. /help for commands.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            if cmd in ("/quit", "/exit"):
                return 0
            if cmd == "/help":
                print(HELP)
            elif cmd == "/profile":
                print(json.dumps(jarvis.store.profile(), ensure_ascii=False, indent=2))
            elif cmd == "/memories":
                for m in jarvis.store.recall(rest):
                    print(f"#{m.id} [{m.kind}, {m.importance}] {m.content}")
            elif cmd == "/goals":
                for g in jarvis.store.goals():
                    print(f"#{g.id} {g.title}" + (f" (due {g.deadline})" if g.deadline else ""))
                    for s in jarvis.store.plan(g.id):
                        print(f"   {s.idx}. [{s.status}] {s.description}  (step #{s.id})")
            elif cmd == "/memos":
                for m in jarvis.store.memos(limit=20):
                    imp = m.importance if m.importance is not None else "?"
                    print(f"#{m.id} [{m.status}, 중요도 {imp}, {m.mentions}회] {m.text}")
            elif cmd == "/new":
                jarvis.reset()
                print("new conversation.")
            elif cmd == "/think":
                show_thinking["on"] = not show_thinking["on"]
                print(f"reasoning summary {'on' if show_thinking['on'] else 'off'}.")
            else:
                print(f"unknown command {cmd}. /help for commands.")
            continue
        streamed["any"] = False
        try:
            result = jarvis.ask(line)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print(f"\n[오류] {type(e).__name__}: {e}")
            continue
        if streamed["any"]:
            print()
            if result.stop_reason != "end_turn":  # refusal / truncation notes are added after streaming
                print(result.text.rsplit("\n\n", 1)[-1])
        else:
            print(f"\n{result.text}")


if __name__ == "__main__":
    raise SystemExit(main())
