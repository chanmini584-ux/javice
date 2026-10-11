"""Interactive terminal front end: `python -m jarvis` or `jarvis`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agent import Jarvis
from .config import Settings

HELP = """\
commands:
  /profile            show your stored profile
  /memories [query]   search long-term memory
  /goals              show active goals and plans
  /new                start a fresh conversation (memory is kept)
  /think              toggle showing Claude's reasoning summary
  /quit               exit
"""


def _approve(name: str, tool_input: dict[str, Any]) -> bool:
    print(f"\n[approval] JARVIS wants to run `{name}`:")
    for key, value in tool_input.items():
        print(f"  {key}:\n" + "\n".join("    " + line for line in str(value).splitlines()))
    return input("allow? [y/N] ").strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Personal AI agent on Claude.")
    parser.add_argument("prompt", nargs="*", help="one-shot prompt; omit for interactive mode")
    args = parser.parse_args(argv)

    show_thinking = {"on": False}

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "thinking" and show_thinking["on"]:
            print(f"  (thinking) {data['text']}", file=sys.stderr)
        elif kind == "tool":
            mark = "!" if data["is_error"] else "·"
            print(f"  {mark} {data['name']}({json.dumps(data['input'], ensure_ascii=False)[:120]})", file=sys.stderr)
        elif kind == "server_tool":
            print(f"  · {data['name']}({json.dumps(data['input'], ensure_ascii=False)[:120]})", file=sys.stderr)

    jarvis = Jarvis(Settings.from_env(), approve=_approve, on_event=on_event)

    if args.prompt:
        print(jarvis.ask(" ".join(args.prompt)).text)
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
            elif cmd == "/new":
                jarvis.reset()
                print("new conversation.")
            elif cmd == "/think":
                show_thinking["on"] = not show_thinking["on"]
                print(f"reasoning summary {'on' if show_thinking['on'] else 'off'}.")
            else:
                print(f"unknown command {cmd}. /help for commands.")
            continue
        try:
            result = jarvis.ask(line)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        print(f"\n{result.text}")


if __name__ == "__main__":
    raise SystemExit(main())
