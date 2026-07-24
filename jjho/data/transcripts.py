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
   ``<main>``. ~25 episodes (mostly 2023-era) publish the transcript as a
   downloadable **PDF** instead — the page's ``<main>`` is only a "Download
   transcript (pdf)" stub — so when the inline text is below threshold and the
   page carries a ``wp-content`` PDF link, the PDF is fetched (via
   ``httpclient.fetch_bytes``) and parsed with :mod:`pypdf`.

All fetching goes through :mod:`jjho.data.httpclient` (≥1 req/s, cached,
identified UA, HTTP/1.1, backoff). The step is idempotent and **resumable** —
episodes with a stored transcript body are skipped.
"""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import db
from .httpclient import fetch, fetch_bytes

log = logging.getLogger("jjho.data.transcripts")

LISTING_URL = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
               "?_paged={page}")
MAX_LISTING_PAGES = 120          # safety cap (~12 links/page, ~760 episodes)
MIN_TRANSCRIPT_CHARS = 800       # below this we treat a page as "not a real transcript"
LISTING_PAGE_RETRIES = 3         # transient-fetch retries before skipping a page
# Extraction caps — bound the work a single (possibly hostile) PDF can impose.
MAX_PDF_PAGES = 400              # never iterate more than this many PDF pages
MAX_PDF_TEXT_CHARS = 3_000_000  # truncate extracted text to this ceiling

_EP_IN_URL = re.compile(r"ep-(\d+)", re.IGNORECASE)
# A downloadable-PDF transcript link: an ``.pdf`` under Maximum Fun's uploads.
# (~25 episodes, mostly 2023-era, publish the transcript as a PDF, not inline
# HTML — the page's <main> is only a "Download transcript (pdf)" stub.)
# SSRF-hardened: anchor the HOST directly (https + maximumfun.org/wp-content/),
# so a match can't START on an attacker-controlled host that merely mentions
# "maximumfun.org" in a query/path (e.g. ``https://evil.com/?x=maximumfun.org/
# wp-content/a.pdf`` or ``http://169.254.169.254/latest/maximumfun.org/...``).
_PDF_HREF = re.compile(
    r"https://(?:www\.)?maximumfun\.org/wp-content/[^\s\"']*\.pdf",
    re.IGNORECASE)
# Allowed hosts for a PDF transcript fetch (defensive urlparse check below).
_PDF_ALLOWED_HOSTS = {"maximumfun.org", "www.maximumfun.org"}


def _fetch_listing_page(page: int) -> str | None:
    """Fetch one listing page, retrying transient failures.

    Returns the page HTML, or ``None`` only after ``LISTING_PAGE_RETRIES``
    genuine failures (the httpclient already backs off between attempts). This
    lets the crawler tell a real end-of-listing (a validly-fetched page with no
    links) apart from a transient hiccup — so one flaky fetch never silently
    truncates the newest-first crawl (the non-determinism this hardens against).
    """
    url = LISTING_URL.format(page=page)
    for attempt in range(1, LISTING_PAGE_RETRIES + 1):
        html, _ = fetch(url, force=True)
        if html:
            return html
        if attempt < LISTING_PAGE_RETRIES:
            log.warning("listing page %d empty/failed (attempt %d/%d) — retrying",
                        page, attempt, LISTING_PAGE_RETRIES)
    return None


def build_listing_map(min_number: int = 0) -> dict[int, str]:
    """Crawl listing pages (newest-first) → ``{episode_number: url}``.

    Stops when a validly-fetched page yields **zero** transcript links (a real
    end-of-listing), or once every collected episode number has dropped below
    ``min_number`` (so a small sample only crawls a few pages), or at the
    ``MAX_LISTING_PAGES`` safety cap. ``transcript-…`` slugs win over other
    guides (``recipe-guide-…``) for the same number. Listing pages are fetched
    ``force`` (fresh) since page 1 changes as episodes drop.

    A **transient** fetch failure (``html is None`` after retries) does NOT
    abort the crawl — the page is logged and skipped, and the walk continues to
    older pages. Only a validly-fetched empty page ends it. (An earlier
    ``if not html: break`` conflated the two, so a single flaky page could
    silently drop every older episode — the 189-vs-214 non-determinism.)
    """
    mapping: dict[int, str] = {}
    for page in range(1, MAX_LISTING_PAGES + 1):
        html = _fetch_listing_page(page)
        if html is None:
            # Transient failure after retries: skip this page, keep crawling.
            log.warning("listing page %d unreachable after %d retries — "
                        "skipping (not aborting crawl)", page,
                        LISTING_PAGE_RETRIES)
            continue
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
            break        # genuine end-of-listing (validly fetched, no links)
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


def find_pdf_transcript_url(html: str) -> str | None:
    """Return the wp-content PDF transcript link on a page, or ``None``.

    The ~25 PDF-only pages render a "Download transcript (pdf)" stub whose sole
    real payload is an ``<a href>`` to a ``maximumfun.org/wp-content/…/*.pdf``.
    We match the href directly (regex over the raw HTML) so a missing/rebuilt
    ``<main>`` doesn't hide it.

    SSRF-hardened: the regex anchors the host, and — belt-and-suspenders — the
    matched URL is re-parsed with :func:`urllib.parse.urlparse` and only
    returned if it is ``https://`` to an allowed maximumfun.org host with a
    ``/wp-content/`` path ending in ``.pdf``. Anything else returns ``None`` so
    a crafted link can never steer :func:`fetch_bytes` at an internal/other host.
    """
    m = _PDF_HREF.search(html)
    if not m:
        return None
    url = m.group(0)
    parsed = urlparse(url)
    if (parsed.scheme == "https"
            and (parsed.hostname or "").lower() in _PDF_ALLOWED_HOSTS
            and parsed.path.startswith("/wp-content/")
            and parsed.path.lower().endswith(".pdf")):
        return url
    log.warning("rejecting non-conforming PDF transcript URL: %s", url)
    return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract plain text from a transcript PDF's bytes via :mod:`pypdf`.

    Pure (no network). Returns ``""`` on any pypdf failure (corrupt/encrypted
    PDF) — the caller treats an empty/short result as "no transcript" and never
    crashes the backfill. pypdf is imported lazily so the module still imports
    if the optional dep is absent.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is pinned in requirements
        log.warning("pypdf not installed — cannot extract PDF transcripts")
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        # Cap page count so a PDF with pathologically many pages can't pin the
        # worker. (A pypdf *hang* on a single page can't be caught here — the
        # byte cap in fetch_bytes is the primary mitigation; this bounds the
        # page-count/text dimensions.)
        pages = [(page.extract_text() or "")
                 for page in reader.pages[:MAX_PDF_PAGES]]
    except Exception as exc:  # pypdf raises a variety of parse errors
        log.warning("pypdf failed to parse transcript PDF (%d bytes): %s",
                    len(pdf_bytes), exc)
        return ""
    text = "\n\n".join(p.strip() for p in pages if p and p.strip())
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > MAX_PDF_TEXT_CHARS:
        log.warning("PDF text (%d chars) exceeds cap — truncating to %d",
                    len(text), MAX_PDF_TEXT_CHARS)
        text = text[:MAX_PDF_TEXT_CHARS]
    return text


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
        source_url = url
        # PDF fallback: a sub-threshold <main> that carries a wp-content PDF
        # link is one of the ~25 PDF-only transcript pages — fetch + parse it.
        if len(text) < MIN_TRANSCRIPT_CHARS:
            pdf_url = find_pdf_transcript_url(html)
            if pdf_url:
                log.info("ep %s: <main> is a PDF stub — fetching %s",
                         t["number"], pdf_url)
                pdf_bytes, _ = fetch_bytes(pdf_url)
                if pdf_bytes:
                    pdf_text = extract_pdf_text(pdf_bytes)
                    if len(pdf_text) >= MIN_TRANSCRIPT_CHARS:
                        text, source_url = pdf_text, pdf_url
        ok = len(text) >= MIN_TRANSCRIPT_CHARS
        db.upsert_transcript(conn, t["id"], text if ok else None, source_url,
                             has_transcript=ok)
        if ok:
            with_transcript += 1
    conn.commit()
    log.info("transcripts: sampled %d, with transcript %d (%d newly fetched, "
             "%d already on file)", sampled, with_transcript, fetched, skipped)
    return {"sampled": sampled, "with_transcript": with_transcript,
            "newly_fetched": fetched, "skipped": skipped}
