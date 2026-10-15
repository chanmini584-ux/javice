"""Where the daily collector reads from. Edit ~/.jarvis/sources.json to change it.

Feeds (RSS/Atom) are preferred over scraping pages: they are meant to be read by
programs, change rarely, and do not run into site terms the way page scraping can.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

TOPICS = ("it", "math", "mech", "daily")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    topic: str  # one of TOPICS
    fetch_full_text: bool = False  # also download the linked article (obeys robots.txt)
    max_items: int = 30


DEFAULT_SOURCES = [
    # IT / software / AI
    Source("GeekNews", "https://news.hada.io/rss/news", "it", fetch_full_text=True),
    Source("Hacker News", "https://hnrss.org/frontpage", "it"),
    Source("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", "it", max_items=20),
    Source("arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", "it", max_items=20),
    # Mathematics
    Source("Quanta Magazine", "https://www.quantamagazine.org/feed/", "math", fetch_full_text=True),
    Source("arXiv math.HO (history & overview)", "https://rss.arxiv.org/rss/math.HO", "math", max_items=10),
    # Mechanical engineering / robotics / control
    Source("arXiv cs.RO (robotics)", "https://rss.arxiv.org/rss/cs.RO", "mech", max_items=15),
    Source("arXiv eess.SY (systems & control)", "https://rss.arxiv.org/rss/eess.SY", "mech", max_items=15),
    Source("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss", "mech"),
    # Everyday news (Korea)
    Source("연합뉴스 최신", "https://www.yna.co.kr/rss/news.xml", "daily"),
]


def load_sources(path: Path) -> list[Source]:
    """Read sources.json, creating it from the defaults on first run."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(s) for s in DEFAULT_SOURCES], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return list(DEFAULT_SOURCES)
    return [Source(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
