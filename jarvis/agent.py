"""The agent loop: Claude + memory + tools, with an approval gate for risky actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from .config import Settings
from .memory import MemoryStore
from .prompts import SYSTEM_PROMPT, situation_block
from .tools import ToolRegistry, register_builtin_tools

ApproveFn = Callable[[str, dict[str, Any]], bool]
EventFn = Callable[[str, dict[str, Any]], None]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _deny_all(name: str, tool_input: dict[str, Any]) -> bool:
    return False


def _ignore(kind: str, data: dict[str, Any]) -> None:
    pass


@dataclass
class TurnResult:
    text: str
    stop_reason: str
    steps: int
    tool_calls: list[str] = field(default_factory=list)


class Jarvis:
    def __init__(
        self,
        settings: Settings | None = None,
        store: MemoryStore | None = None,
        client: Any | None = None,
        approve: ApproveFn = _deny_all,
        on_event: EventFn = _ignore,
        eyes: Any | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.store = store or MemoryStore(self.settings.db_path)
        self.client = client or anthropic.Anthropic()
        self.approve = approve
        self.on_event = on_event
        self.registry = ToolRegistry()
        register_builtin_tools(self.registry, self.store)
        self.eyes = eyes
        if eyes is not None:
            eyes.register_tools(self.registry)
        # Conversation history is append-only: thinking blocks are bound to the exact
        # history that produced them, so earlier turns are never edited.
        self.messages: list[dict[str, Any]] = []

    # -- request ---------------------------------------------------------------

    def _tools(self) -> list[dict[str, Any]]:
        tools = self.registry.definitions()
        if self.settings.enable_web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})
        return tools

    def _request(self) -> Any:
        params = dict(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=self._tools(),
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.effort},
            # On a safety-classifier refusal, the API retries on a fallback model for us.
            betas=[FALLBACK_BETA],
            fallbacks="default",
            # Cache the growing conversation prefix between steps and turns.
            cache_control={"type": "ephemeral"},
        )
        with self.client.beta.messages.stream(**params) as stream:
            return stream.get_final_message()

    # -- loop ------------------------------------------------------------------

    def ask(self, text: str) -> TurnResult:
        mark = len(self.messages)
        try:
            return self._ask(text)
        except BaseException:
            # Drop the unfinished turn so history never ends on a dangling tool_use.
            del self.messages[mark:]
            raise

    def _ask(self, text: str) -> TurnResult:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": situation_block(self.store)},
                    {"type": "text", "text": text},
                ],
            }
        )
        calls: list[str] = []
        response = None
        for step in range(1, self.settings.max_steps + 1):
            response = self._request()
            self.messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    self.on_event("thinking", {"text": block.thinking})
                elif block.type == "server_tool_use":
                    self.on_event("server_tool", {"name": block.name, "input": block.input})

            reason = response.stop_reason
            if reason == "tool_use":
                self.messages.append({"role": "user", "content": self._run_tools(response.content, calls)})
                continue
            if reason == "pause_turn":  # server tool (web search) hit its iteration cap
                continue
            if reason == "refusal":
                details = getattr(response, "stop_details", None)
                why = getattr(details, "explanation", None) or "the request was declined"
                return TurnResult(f"[refused] {why}", reason, step, calls)
            text = _text_of(response.content)
            if reason == "max_tokens":
                text += "\n\n[truncated: hit max_tokens]"
            return TurnResult(text, reason or "end_turn", step, calls)

        text = _text_of(response.content) if response else ""
        return TurnResult(text + "\n\n[stopped: step limit reached]", "max_steps", self.settings.max_steps, calls)

    def _run_tools(self, content: list[Any], calls: list[str]) -> list[dict[str, Any]]:
        # All results go back in ONE user message, in order, including errors.
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            calls.append(block.name)
            tool_input = dict(block.input or {})
            tool = self.registry.get(block.name)
            if tool and tool.risk == "confirm" and not self.approve(block.name, tool_input):
                output, is_error = "The user declined this action. Do not retry it; ask or adapt.", True
            else:
                output, is_error = self.registry.execute(block.name, tool_input)
            self.on_event("tool", {"name": block.name, "input": tool_input, "output": output, "is_error": is_error})
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
            )
        return results

    def reset(self) -> None:
        """Start a new conversation; long-term memory is kept."""
        self.messages = []


def _text_of(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()
