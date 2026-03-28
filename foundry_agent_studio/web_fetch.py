"""Fetch public http(s) URLs for world wiki content (server-side; SSRF-hardened)."""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 2_000_000
MAX_STORED_TEXT_CHARS = 500_000
# Max chars of cached wiki text injected into one system prompt (rest truncated).
MAX_PROMPT_WIKI_CHARS = 120_000
USER_AGENT = "FoundryAgentStudio/1.0 (+local wiki fetch; contact table owner)"


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip = False
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _assert_public_http_url(url: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("Invalid URL (no host)")
    hl = host.lower()
    if hl in ("localhost",) or hl.endswith(".localhost"):
        raise ValueError("Local hosts are not allowed")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host: {e}") from e
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("URL resolves to a non-public address (SSRF protection)")
        if ip.version == 4 and str(ip) == "169.254.169.254":
            raise ValueError("Blocked address")


def html_to_text(html: str) -> str:
    p = _HTMLToText()
    try:
        p.feed(html)
        p.close()
        return p.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def fetch_url_text(url: str) -> str:
    """
    GET url, return plain text (HTML stripped). Raises on HTTP errors / SSRF / size.
    """
    _assert_public_http_url(url)
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        limits=httpx.Limits(max_connections=1),
    ) as client:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("Response too large")
                chunks.append(chunk)
    raw_bytes = b"".join(chunks)
    ct = content_type
    charset = "utf-8"
    if "charset=" in ct:
        m = re.search(r"charset=([\w-]+)", ct)
        if m:
            charset = m.group(1).strip() or "utf-8"
    try:
        text = raw_bytes.decode(charset, errors="replace")
    except LookupError:
        text = raw_bytes.decode("utf-8", errors="replace")

    if "html" in ct or text.lstrip().lower().startswith("<!doctype html") or text.lstrip().startswith("<html"):
        plain = html_to_text(text)
    else:
        plain = text

    if len(plain) > MAX_STORED_TEXT_CHARS:
        plain = plain[: MAX_STORED_TEXT_CHARS - 30] + "\n… (truncated at storage limit)"
    return plain
