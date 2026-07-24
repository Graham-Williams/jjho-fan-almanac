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
# Hard cap on a downloaded body (transcript PDFs). Bounds memory so a
# gzip/decompression bomb or a runaway response can't OOM the worker.
MAX_PDF_BYTES = 25 * 1024 * 1024   # 25 MB
_DOWNLOAD_CHUNK = 64 * 1024        # 64 KB streamed read size

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

    Security hardening (transcript PDFs are attacker-influencable content):

    - **No redirect-laundering.** We pass ``allow_redirects=False`` — the
      cleanest guarantee given this is a generic client, since it means the
      byte fetch can NEVER be steered by a 30x ``Location`` toward an internal
      host (SSRF). Legit wp-content uploads are served directly (200, no hop);
      a redirect is treated as a non-200 "giving up" and returns ``None``.
    - **Hard size cap** (``MAX_PDF_BYTES``). We reject up front on an oversized
      ``Content-Length`` and, since that header is advisory, ``stream=True`` and
      abort once the accumulated body exceeds the cap — so a decompression bomb
      can't OOM the worker. Only a fully-downloaded, under-cap body is cached;
      a partial/aborted download is never written to the ``.bin`` cache.
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
            # allow_redirects=False blocks SSRF via a laundered 30x Location.
            # stream=True lets us enforce the byte cap while downloading.
            resp = sess.get(url, headers=headers, timeout=TIMEOUT,
                            allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            log.warning("request error (%s/%s) %s: %s", attempt,
                        MAX_RETRIES, url, exc)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            # Reject up front if the (advisory) Content-Length is over the cap.
            clen = resp.headers.get("Content-Length")
            if clen is not None:
                try:
                    if int(clen) > MAX_PDF_BYTES:
                        log.warning("PDF %s Content-Length %s exceeds cap %d "
                                    "— aborting", url, clen, MAX_PDF_BYTES)
                        resp.close()
                        return None, resp.status_code
                except ValueError:
                    pass
            # Stream + enforce the cap (Content-Length can lie / be absent).
            chunks: list[bytes] = []
            total = 0
            over_cap = False
            try:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        log.warning("PDF %s exceeded %d bytes mid-download "
                                    "— aborting (not cached)", url,
                                    MAX_PDF_BYTES)
                        over_cap = True
                        break
                    chunks.append(chunk)
            except requests.RequestException as exc:
                log.warning("stream error (%s/%s) %s: %s", attempt,
                            MAX_RETRIES, url, exc)
                resp.close()
                time.sleep(backoff)
                backoff *= 2
                continue
            finally:
                resp.close()
            if over_cap:
                # Do NOT cache a partial/aborted download.
                return None, resp.status_code
            body = b"".join(chunks)
            cf.write_bytes(body)
            return body, 200
        if resp.status_code == 404:
            resp.close()
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
            resp.close()
            time.sleep(wait)
            backoff *= 2
            continue
        # Any other status (incl. a blocked 30x redirect) — give up.
        log.warning("HTTP %s on %s (giving up)", resp.status_code, url)
        status = resp.status_code
        resp.close()
        return None, status
    return None, 0
