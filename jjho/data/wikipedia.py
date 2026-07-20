"""Wikipedia enrichment — guest bailiff + dispute description (see DESIGN.md).

Scrapes the two "List of Judge John Hodgman episodes" pages (2010-2014 and
2015-present) with BeautifulSoup. Each episode table has columns
``No. | Episode Title | Guest Bailiff | Dispute | Release date``. Rows are
merged onto the RSS spine by **episode number** first, falling back to a
**normalized-title** fuzzy match for the handful of RSS items that carry no
number.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from . import db
from .httpclient import fetch

log = logging.getLogger("jjho.data.wikipedia")

LIST_PAGES = [
    "https://en.wikipedia.org/wiki/"
    "List_of_Judge_John_Hodgman_episodes_(2010%E2%80%932014)",
    "https://en.wikipedia.org/wiki/"
    "List_of_Judge_John_Hodgman_episodes_(2015%E2%80%93present)",
]

_PLACEHOLDER_DISPUTES = {"docket episode", "docket episodes", "n/a", "—",
                         "-", "", "tbd"}


def normalize_title(title: str) -> str:
    """Lowercase, strip quotes/punctuation/whitespace for fuzzy matching."""
    t = title.lower()
    t = t.replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"')
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _header_map(table) -> dict | None:
    """Map an episode table's columns.

    Handles both list formats: the 2015-present tables
    (``No. | Episode Title | Guest Bailiff | Dispute | Release date``) and the
    2010-2014 tables (``No. | Title | Original release date`` with the dispute
    in a following colspan sub-row). The year-summary table has neither a
    ``No.`` nor a ``Title`` column and is skipped.
    """
    header_row = table.find("tr")
    if not header_row:
        return None
    headers = [c.get_text(" ", strip=True).lower()
               for c in header_row.find_all(["th", "td"])]
    idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h.startswith("no") and "number" not in idx:
            idx["number"] = i
        elif "title" in h and "title" not in idx:
            idx["title"] = i
        elif "guest bailiff" in h:
            idx["guest"] = i
        elif "dispute" in h:
            idx["dispute"] = i
    return idx if "number" in idx and "title" in idx else None


def _clean(text: str) -> str:
    text = re.sub(r"\[\s*\d+\s*\]", "", text)     # strip footnote markers [17]
    text = re.sub(r"\(\s*\d{4}-\d{2}-\d{2}\s*\)", "", text)  # iso date artifact
    text = text.strip().strip('"').strip("“”").strip()
    return re.sub(r"\s+", " ", text).strip()


def _is_description_row(tr, number_idx: int) -> bool:
    """A colspan sub-row carrying the episode's dispute description."""
    cells = tr.find_all(["th", "td"])
    if len(cells) != 1:
        return False
    span = cells[0].get("colspan")
    return bool(span) or number_idx == 0


def parse_rows() -> list[dict]:
    """Return enrichment rows: number, title, guest_bailiff, dispute."""
    rows: list[dict] = []
    for url in LIST_PAGES:
        html, status = fetch(url)
        if not html:
            log.warning("Wikipedia page unavailable (HTTP %s): %s", status, url)
            continue
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.select("table.wikitable"):
            idx = _header_map(table)
            if not idx:
                continue
            pending: dict | None = None
            for tr in table.find_all("tr")[1:]:
                # Attach a colspan description sub-row to the row above it.
                if pending is not None and _is_description_row(tr, idx["number"]):
                    desc = _clean(tr.get_text(" ", strip=True))
                    if desc and desc.lower() not in _PLACEHOLDER_DISPUTES \
                            and not pending["dispute"]:
                        pending["dispute"] = desc
                    rows.append(pending)
                    pending = None
                    continue
                if pending is not None:      # no sub-row followed the last one
                    rows.append(pending)
                    pending = None

                cells = tr.find_all(["th", "td"])
                if len(cells) <= idx["title"]:
                    continue
                num_txt = _clean(cells[idx["number"]].get_text(" ", strip=True))
                m = re.match(r"(\d+)", num_txt)
                number = int(m.group(1)) if m else None
                title = _clean(cells[idx["title"]].get_text(" ", strip=True))
                if not title:
                    continue
                guest = (_clean(cells[idx["guest"]].get_text(" ", strip=True))
                         if "guest" in idx and len(cells) > idx["guest"] else "")
                dispute = (_clean(cells[idx["dispute"]].get_text(" ", strip=True))
                           if "dispute" in idx and len(cells) > idx["dispute"]
                           else "")
                if dispute.lower() in _PLACEHOLDER_DISPUTES:
                    dispute = ""
                pending = {"number": number, "title": title,
                           "guest_bailiff": guest, "dispute": dispute}
            if pending is not None:
                rows.append(pending)
    return rows


def ingest(conn) -> dict:
    """Merge Wikipedia rows onto the RSS spine. Returns a summary dict.

    **Matched by normalized title, NOT by number.** The RSS ``itunes:episode``
    numbers and Wikipedia's ``No.`` column diverge (Wikipedia counts an early
    pilot/specials differently, so its numbering runs ~2 ahead through the back
    catalog). Titles are the reliable join key (~96% match); RSS numbering is
    kept authoritative for the spine.
    """
    wiki_rows = parse_rows()

    by_title: dict[str, str] = {}
    for r in conn.execute("SELECT id, title FROM episodes").fetchall():
        by_title.setdefault(normalize_title(r["title"]), r["id"])

    matched = 0
    unmatched = 0
    for w in wiki_rows:
        ep_id = by_title.get(normalize_title(w["title"]))
        if ep_id is None:
            unmatched += 1
            continue
        db.enrich_episode_wikipedia(conn, ep_id, w["guest_bailiff"],
                                    w["dispute"])
        matched += 1
    conn.commit()
    log.info("Wikipedia: %d table rows (%d matched by title, %d unmatched)",
             len(wiki_rows), matched, unmatched)
    return {"total": len(wiki_rows), "matched": matched,
            "unmatched": unmatched}
