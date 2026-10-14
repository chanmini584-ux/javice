"""Daily collector: read feeds, optionally fetch article text, store in the knowledge base.

Polite by default: identifies itself, obeys robots.txt for article pages, waits
between requests to the same host, caps download size, and skips what it already has.

Safe by default: feed items link wherever their authors like, so only http(s) URLs
that resolve to public internet addresses are fetched, redirects included. A feed
cannot make JARVIS read local files or probe the home network (router, other PCs).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import time
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

from .sources import Source
from .store import KnowledgeBase

USER_AGENT = "JARVIS-personal-collector/0.1 (+personal use; respects robots.txt)"
MAX_BYTES = 2_000_000
WEB_SCHEMES = ("http", "https")

Fetch = Callable[[str], bytes]
Resolve = Callable[[str, int], list[str]]


class UnsafeURLError(ValueError):
    """The URL is not http(s) or points at this machine or a private network."""


def _resolve(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr in _NAT64 or addr in _IPV4_COMPATIBLE:  # an IPv4 address carried inside IPv6
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr.is_global and not addr.is_multicast


def check_url(url: str, resolve: Resolve = _resolve, proxied: bool = False) -> None:
    """Raise UnsafeURLError unless url is http(s) and its host resolves only to public addresses.

    proxied: a proxy makes the connection, so a name that only the proxy can resolve is let
    through; literal addresses and names that resolve here are still checked.
    """
    parts = urlparse(url)
    host = parts.hostname
    if parts.scheme not in WEB_SCHEMES or not host:
        raise UnsafeURLError(f"only http(s) URLs are fetched: {url!r}")
    if "%" in host:  # urllib connects to the decoded name, not the one checked here
        raise UnsafeURLError(f"encoded host name in {url!r}")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"bad port in {url!r}") from exc
    try:
        ips = resolve(host, port)
    except OSError:
        if proxied:
            return
        raise
    for ip in ips:
        if not _is_public(ip):
            raise UnsafeURLError(f"{host} points to a non-public address ({ip})")


def _connect_public(address, *args, **kwargs) -> socket.socket:
    """socket.create_connection that hangs up on a non-public peer before anything is sent.

    It checks the address actually connected to: a name can pass check_url and still lead
    elsewhere (DNS rebinding, a name that urllib rewrites before connecting).
    """
    sock = socket.create_connection(address, *args, **kwargs)
    peer = sock.getpeername()[0]
    if not _is_public(peer):
        sock.close()
        raise UnsafeURLError(f"{address[0]} connected to a non-public address ({peer})")
    return sock


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request):
        if req.has_proxy():  # the peer is the proxy; check_url vetted the target by name
            return super().http_open(req)
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request):
        if getattr(req, "_tunnel_host", None):  # CONNECT through a proxy: same as above
            return super().https_open(req)
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _PublicOnly(urllib.request.BaseHandler):
    """Checks every request, including each redirect hop, before anything is sent."""

    def __init__(self, resolve: Resolve) -> None:
        self.resolve = resolve

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        proxied = req.type in urllib.request.getproxies() and not urllib.request.proxy_bypass(req.host)
        check_url(req.full_url, self.resolve, proxied)
        return req

    https_request = http_request

    def unknown_open(self, req: urllib.request.Request) -> None:
        raise UnsafeURLError(f"only http(s) URLs are fetched: {req.full_url!r}")


def public_opener(resolve: Resolve = _resolve) -> urllib.request.OpenerDirector:
    """An opener that speaks only http(s) to public hosts (no file:, ftp: or data: handlers)."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        _PublicOnly(resolve),
        urllib.request.ProxyHandler(),
        _PublicHTTPHandler(),
        _PublicHTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def http_fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with public_opener().open(req, timeout=timeout) as resp:
        return resp.read(MAX_BYTES)


# --- feed parsing ------------------------------------------------------------------


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: datetime | None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom: RFC 3339
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(data: bytes) -> list[FeedItem]:
    """Parse RSS 2.0, RSS 1.0 (RDF, used by arXiv) or Atom."""
    root = ET.fromstring(data)
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for it in items:
        link = ""
        for c in it:
            if _local(c.tag) == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if not href and (c.text or "").strip():
                    link = c.text.strip()
                    break
        summary = _text(_child(it, "description", "summary", "content", "encoded"))
        published = _date(_text(_child(it, "pubDate", "published", "updated", "date")))
        title = _text(_child(it, "title"))
        if link and title:
            out.append(FeedItem(title, link, html_to_text(summary), published))
    return out


# --- HTML to text --------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"}
    BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote", "article", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if "<" not in html:
        return " ".join(html.split())
    p = _TextExtractor()
    p.feed(html)
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


def extract_article(html: str, url: str) -> str:
    """Main-text extraction: trafilatura when installed, otherwise a plain HTML-to-text pass."""
    try:
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
        if text:
            return text
    except ImportError:
        pass
    return html_to_text(html)


# --- collector -----------------------------------------------------------------------


@dataclass
class CollectReport:
    new: int = 0
    seen: int = 0
    failed_sources: list[str] = field(default_factory=list)
    blocked_by_robots: int = 0
    unsafe: int = 0  # links refused by check_url: not http(s), or pointing at a private network

    def summary(self) -> str:
        s = f"new {self.new}, already had {self.seen}, robots-blocked {self.blocked_by_robots}"
        if self.unsafe:
            s += f", unsafe {self.unsafe}"
        if self.failed_sources:
            s += f", failed sources: {', '.join(self.failed_sources)}"
        return s


class Collector:
    def __init__(
        self,
        kb: KnowledgeBase,
        fetch: Fetch = http_fetch,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        max_text_chars: int = 20_000,
    ) -> None:
        self.kb = kb
        self.fetch = fetch
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self.max_text_chars = max_text_chars
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _polite_fetch(self, url: str) -> bytes:
        host = urlparse(url).netloc
        wait = self._last_hit.get(host, 0) + self.min_interval_s - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        try:
            return self.fetch(url)
        finally:
            self._last_hit[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.parse(self._polite_fetch(base + "/robots.txt").decode("utf-8", "replace").splitlines())
                self._robots[base] = rp
            except Exception:
                self._robots[base] = None  # no robots.txt reachable: treat as allowed
        rp = self._robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def collect(self, sources: list[Source], log: Callable[[str], None] = lambda s: None) -> CollectReport:
        report = CollectReport()
        for src in sources:
            try:
                items = parse_feed(self._polite_fetch(src.url))[: src.max_items]
            except Exception as exc:
                report.failed_sources.append(src.name)
                log(f"{src.name}: {exc}")
                continue
            for item in items:
                link = urljoin(src.url, item.link)  # "/post/1" belongs to the feed's own site
                if urlparse(link).scheme not in WEB_SCHEMES:
                    report.unsafe += 1  # file:, ftp:, javascript:… never fetched, never stored
                    continue
                if self.kb.has(link):
                    report.seen += 1
                    continue
                text = item.summary
                if src.fetch_full_text:
                    if self.allowed(link):
                        try:
                            html = self._polite_fetch(link).decode("utf-8", "replace")
                            text = extract_article(html, link) or text
                        except UnsafeURLError as exc:  # keep the feed summary only
                            report.unsafe += 1
                            log(f"{link}: {exc}")
                        except Exception as exc:
                            log(f"{link}: {exc}")
                    else:
                        report.blocked_by_robots += 1
                self.kb.add(link, src.name, src.topic, item.title, text[: self.max_text_chars], item.published)
                report.new += 1
        return report
