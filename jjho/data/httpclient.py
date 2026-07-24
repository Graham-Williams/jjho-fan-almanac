"""Polite, cached HTTP client for scraping (mirrors taste-twin's discipline).

Rules (see ``CLAUDE.md`` — never weaken without explicit approval):

- **≥1 request/second**, single-threaded (a process-wide min interval).
- **On-disk cache** under ``data/cache/`` (gitignored) keyed by URL hash — a
  re-run never re-fetches a page it already has.
- **Identified User-Agent** (this is a fan project, not a stealth crawler).
- **Plain ``requests`` / HTTP/1.1** — not httpx/HTTP2 (some CDNs fingerprint
  HTTP/2 clients; taste-twin learned this the hard way with Letterboxd).
- **Backoff** on 429/5xx honoring ``Retry-After``.

Robots.txt is respected by the callers, which only fetch paths the site allows
(Maximum Fun's robots allows the transcript pages; only a few bonus feeds are
disallowed).
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

from .db import data_dir

log = logging.getLogger("jjho.data.http")

CACHE_DIR = data_dir() / "cache"
USER_AGENT = (
    "jjho-fan-almanac/0.1 (unofficial Judge John Hodgman fan project; "
    "polite cached scraper; +https://github.com/Graham-Williams/jjho-fan-almanac)"
)
MIN_INTERVAL = 1.1          # seconds between live requests (≥1s + margin)
MAX_RETRIES = 4
TIMEOUT = 30

_last_request_at = 0.0


def _cache_file(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.html"


def _cache_file_bin(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.bin"


def _throttle() -> None:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_at = time.time()


def fetch(url: str, *, session: requests.Session | None = None,
          force: bool = False) -> tuple[str | None, int]:
    """Return ``(html_or_None, status)``.

    Cache hits return ``(html, 200)`` with no network call. A cached miss is
    also honored (a stored empty file means "known 404" and is not refetched).
    On a hard 404 we cache an empty file so the next run skips it (resumable).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = _cache_file(url)
    if cf.exists() and not force:
        body = cf.read_text(encoding="utf-8")
        return (body or None, 200 if body else 404)

    sess = session or requests
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    backoff = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            # HTTP/1.1 via plain requests; do not swap in an HTTP/2 client.
            resp = sess.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log.warning("request error (%s/%s) %s: %s", attempt,
                        MAX_RETRIES, url, exc)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            cf.write_text(resp.text, encoding="utf-8")
            return resp.text, 200
        if resp.status_code == 404:
            cf.write_text("", encoding="utf-8")   # cache the miss (resumable)
            return None, 404
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = backoff
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    wait = max(wait, float(ra))
                except ValueError:
                    pass
            log.warning("HTTP %s on %s — backoff %.1fs (%s/%s)",
                        resp.status_code, url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            backoff *= 2
            continue
        log.warning("HTTP %s on %s (giving up)", resp.status_code, url)
        return None, resp.status_code
    return None, 0


def fetch_bytes(url: str, *, session: requests.Session | None = None,
                force: bool = False) -> tuple[bytes | None, int]:
    """Return ``(raw_bytes_or_None, status)`` — the binary sibling of :func:`fetch`.

    Mirrors :func:`fetch`'s politeness exactly (the ≥1.1s ``_throttle``, on-disk
    cache keyed by URL hash but with a ``.bin`` extension, retry/backoff honoring
    ``Retry-After``, same identified User-Agent, plain-``requests`` HTTP/1.1).
    Used for transcript **PDFs**, which :func:`fetch` would corrupt by decoding
    to text. A cached zero-length file means "known miss" and is not refetched.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = _cache_file_bin(url)
    if cf.exists() and not force:
        body = cf.read_bytes()
        return (body or None, 200 if body else 404)

    sess = session or requests
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/pdf,application/octet-stream,*/*"}
    backoff = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            # HTTP/1.1 via plain requests; do not swap in an HTTP/2 client.
            resp = sess.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log.warning("request error (%s/%s) %s: %s", attempt,
                        MAX_RETRIES, url, exc)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            cf.write_bytes(resp.content)
            return resp.content, 200
        if resp.status_code == 404:
            cf.write_bytes(b"")                   # cache the miss (resumable)
            return None, 404
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = backoff
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    wait = max(wait, float(ra))
                except ValueError:
                    pass
            log.warning("HTTP %s on %s — backoff %.1fs (%s/%s)",
                        resp.status_code, url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            backoff *= 2
            continue
        log.warning("HTTP %s on %s (giving up)", resp.status_code, url)
        return None, resp.status_code
    return None, 0
