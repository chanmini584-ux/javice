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
# On-demand compaction: the server replaces a long history with a summary block.
COMPACT_BETA = "compact-2026-09-04"
COMPACT_INSTRUCTIONS = (
    "Summarize this conversation between the user and JARVIS, their personal assistant, so it can "
    "continue from the summary alone. Keep what the user asked and decided, open requests and "
    "promises, names, numbers, dates and times, the ids of goals, plan steps, schedule items and "
    "memories that came up, and what tools found. Drop small talk. Write in the user's language. "
    "Do not call tools; reply with the summary text only."
)
# After a failed compaction, wait until the conversation has grown this much before paying for
# another try.
COMPACT_RETRY_TOKENS = 20_000

# The only tools a read-only (unattended) turn may see or run. Unattended runs such as the
# morning briefing read text strangers wrote (feed titles, search results) with nobody
# watching, so nothing that changes memory, schedule or the outside world is offered.
# A new tool stays out until it is added here.
READ_ONLY_TOOLS = frozenset(
    {"recall", "search_memos", "list_goals", "list_schedule", "calculate", "search_knowledge", "look"}
)


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
        knowledge: Any | None = None,
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
        self.knowledge = knowledge
        if knowledge is not None:
            from .knowledge.tools import register_knowledge_tools

            register_knowledge_tools(self.registry, knowledge)
        # Conversation history is append-only: thinking blocks are bound to the exact
        # history that produced them, so earlier turns are never edited (only replaced
        # whole by a compaction summary).
        self.messages: list[dict[str, Any]] = []
        self._last_usage: Any = None
        self._last_readonly = False
        self._compact_retry = (0, 0)  # (id of the history list, tokens it must reach first)

    # -- request ---------------------------------------------------------------

    def _tools(self, readonly: bool = False) -> list[dict[str, Any]]:
        tools = [t for t in self.registry.definitions() if not readonly or t["name"] in READ_ONLY_TOOLS]
        if self.settings.enable_web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})
        return tools

    def _params(self, readonly: bool) -> dict[str, Any]:
        """Settings shared by turns and compaction: identical settings let both hit the prompt cache."""
        return dict(
            model=self.settings.model,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=self._tools(readonly),
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.effort},
            # Cache the growing conversation prefix between steps and turns.
            cache_control={"type": "ephemeral"},
        )

    def _request(self, readonly: bool = False) -> Any:
        params = dict(
            self._params(readonly),
            max_tokens=self.settings.max_tokens,
            # On a safety-classifier refusal, the API retries on a fallback model for us.
            # A history that starts with a compaction block needs the compaction beta.
            betas=[FALLBACK_BETA] + ([COMPACT_BETA] if self._starts_with_summary() else []),
            fallbacks="default",
        )
        with self.client.beta.messages.stream(**params) as stream:
            # Forward text as it is generated so chat UIs can show the answer typing out.
            for chunk in getattr(stream, "text_stream", ()):
                self.on_event("text", {"text": chunk})
            return stream.get_final_message()

    # -- loop ------------------------------------------------------------------

    def ask(self, text: str, readonly: bool = False) -> TurnResult:
        """Run one user turn. Front ends call compact_if_long() after delivering the answer.

        readonly: an unattended turn over untrusted text; only READ_ONLY_TOOLS (plus web
        search, which changes nothing here) are offered and run.
        """
        mark = len(self.messages)
        try:
            return self._ask(text, readonly)
        except BaseException:
            # Drop the unfinished turn so history never ends on a dangling tool_use.
            del self.messages[mark:]
            raise

    def _ask(self, text: str, readonly: bool = False) -> TurnResult:
        self._last_readonly = readonly
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
            response = self._request(readonly)
            self._last_usage = getattr(response, "usage", None)
            self.messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    self.on_event("thinking", {"text": block.thinking})
                elif block.type == "server_tool_use":
                    self.on_event("server_tool", {"name": block.name, "input": block.input})

            reason = response.stop_reason
            if reason == "tool_use":
                self.messages.append({"role": "user", "content": self._run_tools(response.content, calls, readonly)})
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

    def _run_tools(self, content: list[Any], calls: list[str], readonly: bool = False) -> list[dict[str, Any]]:
        # All results go back in ONE user message, in order, including errors.
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            calls.append(block.name)
            tool_input = dict(block.input or {})
            tool = self.registry.get(block.name)
            if readonly and block.name not in READ_ONLY_TOOLS:
                output, is_error = "Not available in an unattended run: only reading tools are allowed.", True
            elif tool and tool.risk == "confirm" and not self.approve(block.name, tool_input):
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

    # -- compaction --------------------------------------------------------------

    def _starts_with_summary(self) -> bool:
        first = self.messages[0]["content"] if self.messages else None
        return isinstance(first, list) and any(_block_type(b) == "compaction" for b in first)

    def compact_if_long(self) -> str | None:
        """Let the server replace a long conversation with a summary. Call it once an answer is out.

        Quality and cost both suffer as a conversation grows. Returns what happened, or None
        for a short conversation. The list is changed in place because the web app keeps one
        history list per chat. A failure leaves the history as it was.
        """
        tokens = _context_tokens(self._last_usage)
        limit = self.settings.compact_at_tokens
        retry_list, retry_at = self._compact_retry
        if not limit or tokens < limit or (retry_list == id(self.messages) and tokens < retry_at):
            return None
        try:
            response = self.client.beta.messages.create(
                **self._params(self._last_readonly),
                max_tokens=16000,  # covers the model's thinking before it writes the summary
                betas=[COMPACT_BETA],
                compaction={"type": "summarize", "instructions": COMPACT_INSTRUCTIONS},
            )
            if response.stop_reason != "compaction" or not any(_block_type(b) == "compaction" for b in response.content):
                raise RuntimeError(f"no summary came back ({response.stop_reason})")  # e.g. a refusal
        except Exception as exc:
            self._compact_retry = (id(self.messages), tokens + COMPACT_RETRY_TOKENS)
            return f"compaction failed: {type(exc).__name__}: {exc}"
        self.messages[:] = [{"role": "assistant", "content": response.content}]
        self._last_usage, self._compact_retry = None, (0, 0)  # the summary starts the count afresh
        return f"compacted a {tokens:,}-token conversation"


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _context_tokens(usage: Any) -> int:
    """Roughly the size of the next request: everything the last one read, plus its output."""
    if usage is None:
        return 0
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    return sum(getattr(usage, f, 0) or 0 for f in fields)


def _text_of(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()
