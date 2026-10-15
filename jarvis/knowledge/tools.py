"""Agent tool over the knowledge base."""

from __future__ import annotations

from ..tools import ToolRegistry
from .sources import TOPICS
from .store import KnowledgeBase


def register_knowledge_tools(registry: ToolRegistry, kb: KnowledgeBase) -> None:
    @registry.tool(
        "search_knowledge",
        "Search the user's personal knowledge base: articles and papers collected daily on "
        "IT/software/AI (it), mathematics (math), mechanical engineering/robotics/control "
        "(mech) and everyday news (daily). Use it before web search for these topics, and "
        "cite the url of what you use. Keywords may be Korean or English; prefer terms of "
        "3+ characters.",
        {
            "query": {"type": "string"},
            "topic": {"type": "string", "enum": ["", *TOPICS]},
        },
        required=["query"],
    )
    def search_knowledge(query: str, topic: str = "") -> list[dict]:
        return [
            {
                "title": d.title,
                "url": d.url,
                "source": d.source,
                "topic": d.topic,
                "published": d.published or d.fetched_at,
                "snippet": snippet,
            }
            for d, snippet in kb.search(query, topic or None)
        ]
