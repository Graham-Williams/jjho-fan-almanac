"""Transcript depth layer — polite Maximum Fun scraper (see DESIGN.md/CLAUDE.md).

Coverage is **partial** (strong for recent years, patchy for older/live
episodes). Two steps:

1. Crawl the paginated transcript listing
   (``maximumfun.org/transcripts/judge-john-hodgman/?_paged=N``) to build an
   ``episode number -> transcript URL`` map. Slugs look like
   ``transcript-judge-john-hodgman-ep-766-power-of-my-turny`` — the episode
   number is embedded, which is how we key them.
2. For each target episode, fetch its transcript page (on-disk cached, so a
   re-run never refetches) and extract the body from the ``<p>`` tags inside
   ``<main>``.

All fetching goes through :mod:`jjho.data.httpclient` (≥1 req/s, cached,
identified UA, HTTP/1.1, backoff). The step is idempotent and **resumable** —
episodes with a stored transcript body are skipped.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from . import db
from .httpclient import fetch

log = logging.getLogger("jjho.data.transcripts")

LISTING_URL = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
               "?_paged={page}")
MAX_LISTING_PAGES = 120          # safety cap (~12 links/page, ~760 episodes)
MIN_TRANSCRIPT_CHARS = 800       # below this we treat a page as "not a real transcript"

_EP_IN_URL = re.compile(r"ep-(\d+)", re.IGNORECASE)


def build_listing_map(min_number: int = 0) -> dict[int, str]:
    """Crawl listing pages (newest-first) → ``{episode_number: url}``.

    Stops when a page yields no transcript links, or once every collected
    episode number has dropped below ``min_number`` (so a small sample only
    crawls a few pages). ``transcript-…`` slugs win over other guides
    (``recipe-guide-…``) for the same number. Listing pages are fetched
    ``force`` (fresh) since page 1 changes as episodes drop.
    """
    mapping: dict[int, str] = {}
    for page in range(1, MAX_LISTING_PAGES + 1):
        html, _ = fetch(LISTING_URL.format(page=page), force=True)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        page_numbers: list[int] = []
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/transcripts/judge-john-hodgman/" not in href:
                continue
            m = _EP_IN_URL.search(href)
            if not m:
                continue
            num = int(m.group(1))
            page_numbers.append(num)
            found += 1
            is_transcript = "/transcript-" in href
            if num not in mapping or (is_transcript
                                      and "/transcript-" not in mapping[num]):
                mapping[num] = href.split("#")[0]
        if found == 0:
            break
        # Once the whole page is older than our lowest target, we can stop.
        if page_numbers and max(page_numbers) < min_number:
            break
    log.info("transcript listing: mapped %d episode numbers across pages",
             len(mapping))
    return mapping


def extract_transcript_text(html: str) -> str:
    """Pull the transcript body (the ``<p>`` tags inside ``<main>``)."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("main") or soup
    paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    paras = [p for p in paras if p]
    text = "\n\n".join(paras)
    return re.sub(r"[ \t]+", " ", text).strip()


def _target_episodes(conn, limit: int | None) -> list[dict]:
    """Most-recent numbered episodes (transcripts are keyed by number)."""
    sql = ("SELECT id, number, title FROM episodes "
           "WHERE number IS NOT NULL "
           "ORDER BY pub_date DESC, number DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def ingest(conn, *, limit: int | None = 25, all_episodes: bool = False) -> dict:
    """Fetch + store transcripts for the target episode set.

    Args:
        limit: number of most-recent episodes to sample (ignored if
            ``all_episodes``).
        all_episodes: scrape every numbered episode (the full backfill).

    Returns a coverage summary.
    """
    targets = _target_episodes(conn, None if all_episodes else limit)
    if not targets:
        return {"sampled": 0, "with_transcript": 0, "skipped": 0}

    min_number = min(t["number"] for t in targets)
    listing = build_listing_map(min_number=min_number)

    sampled = with_transcript = skipped = fetched = 0
    for t in targets:
        sampled += 1
        if db.episode_has_stored_transcript(conn, t["id"]):
            skipped += 1
            with_transcript += 1        # already on file
            continue
        url = listing.get(t["number"])
        if not url:
            db.upsert_transcript(conn, t["id"], None, "", has_transcript=False)
            continue
        html, status = fetch(url)
        if not html:
            db.upsert_transcript(conn, t["id"], None, url, has_transcript=False)
            continue
        fetched += 1
        text = extract_transcript_text(html)
        ok = len(text) >= MIN_TRANSCRIPT_CHARS
        db.upsert_transcript(conn, t["id"], text if ok else None, url,
                             has_transcript=ok)
        if ok:
            with_transcript += 1
    conn.commit()
    log.info("transcripts: sampled %d, with transcript %d (%d newly fetched, "
             "%d already on file)", sampled, with_transcript, fetched, skipped)
    return {"sampled": sampled, "with_transcript": with_transcript,
            "newly_fetched": fetched, "skipped": skipped}
