"""A tiny tool registry: JSON-schema definitions for Claude plus local handlers.

Every tool carries a risk level. ``safe`` tools run immediately; ``confirm``
tools (anything with side effects outside JARVIS's own memory) go through the
approval callback first, so the agent can act on your behalf without acting
behind your back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

Risk = Literal["safe", "confirm"]
Handler = Callable[..., Any]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    risk: Risk = "safe"

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class ToolOutput:
    """A tool result with images attached (base64 JPEG), so Claude can look at them."""

    text: str
    images_b64_jpeg: list[str]

    def blocks(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": self.text}] + [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
            for b in self.images_b64_jpeg
        ]


class ToolError(Exception):
    """Raised by handlers for errors the model should see and recover from."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str] | None = None,
        risk: Risk = "safe",
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`."""

        def wrap(fn: Handler) -> Handler:
            schema = {
                "type": "object",
                "properties": properties,
                "required": required if required is not None else list(properties),
                "additionalProperties": False,
            }
            self.register(Tool(name, description, schema, fn, risk))
            return fn

        return wrap

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        # Sorted so the tools prefix of the prompt stays byte-stable (prompt caching).
        return [self._tools[n].definition() for n in sorted(self._tools)]

    def execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str | list[dict[str, Any]], bool]:
        """Run a tool. Returns (content, is_error); content is a string or content blocks."""
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True
        missing = [k for k in tool.input_schema.get("required", []) if k not in tool_input]
        if missing:
            return f"missing required input(s): {', '.join(missing)}", True
        try:
            result = tool.handler(**tool_input)
        except Exception as exc:  # any tool failure goes back to the model as an error result
            return f"{type(exc).__name__}: {exc}", True
        if isinstance(result, ToolOutput):
            return result.blocks(), False
        if isinstance(result, str):
            return result, False
        return json.dumps(result, ensure_ascii=False, default=str), False
