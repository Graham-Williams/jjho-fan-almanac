"""RSS spine ingest — the complete, cheap index (see ``DESIGN.md``).

Pulls the podcast feed with :mod:`feedparser` and normalizes each item into an
episode row: stable id (guid), episode number (``itunes:episode``, else parsed
from the title), title, publish date, dispute blurb (the item summary), audio
URL (enclosure) and a listen/permalink URL.
"""

from __future__ import annotations

import logging
import re
from calendar import timegm
from datetime import datetime, timezone

import feedparser

from . import db

log = logging.getLogger("jjho.data.rss")

FEED_URL = "https://feeds.simplecast.com/q8x9cVws"

# Fallback: pull a leading/embedded episode number from a title like
# "Episode 782: Road Rules" or "Ep. 500 - ...". Most items carry
# itunes:episode so this is rarely needed.
_TITLE_NUM = re.compile(r"\bep(?:isode|\.)?\s*#?\s*(\d{1,4})\b", re.IGNORECASE)


def _episode_number(entry) -> int | None:
    raw = entry.get("itunes_episode")
    if raw is not None:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            pass
    m = _TITLE_NUM.search(entry.get("title", ""))
    return int(m.group(1)) if m else None


def _pub_date(entry) -> tuple[str | None, str | None]:
    raw = entry.get("published") or entry.get("updated")
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    iso = None
    if parsed:
        iso = datetime.fromtimestamp(timegm(parsed), tz=timezone.utc).isoformat(
            timespec="seconds")
    return iso, raw


def _audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href")
        if href:
            return href
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def _blurb(entry) -> str | None:
    text = entry.get("summary") or entry.get("subtitle") or ""
    text = re.sub(r"<[^>]+>", " ", text)      # strip any HTML
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_entries() -> list[dict]:
    """Fetch + normalize every feed item into episode dicts."""
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS parse failed: {getattr(feed, 'bozo_exception', '?')}")
    rows: list[dict] = []
    for e in feed.entries:
        guid = e.get("id") or e.get("guid") or e.get("link")
        if not guid:
            continue
        iso, raw = _pub_date(e)
        rows.append({
            "id": guid,
            "number": _episode_number(e),
            "title": (e.get("title") or "").strip() or "(untitled)",
            "pub_date": iso,
            "pub_date_raw": raw,
            "blurb": _blurb(e),
            "audio_url": _audio_url(e),
            "listen_url": e.get("link"),
        })
    return rows


def ingest(conn) -> dict:
    """Upsert every RSS item into the index. Returns a summary dict."""
    rows = parse_entries()
    inserted = updated = 0
    for row in rows:
        result = db.upsert_episode_rss(conn, row)
        if result == "inserted":
            inserted += 1
        else:
            updated += 1
    conn.commit()
    numbered = sum(1 for r in rows if r["number"] is not None)
    log.info("RSS: %d entries (%d inserted, %d updated, %d numbered)",
             len(rows), inserted, updated, numbered)
    return {"total": len(rows), "inserted": inserted, "updated": updated,
            "numbered": numbered}
