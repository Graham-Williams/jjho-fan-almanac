"""SQLite index for The Fan Almanac (the spine + transcript store).

The DB lives under ``data/`` and is **gitignored** — it is re-derivable from
public data (RSS + Wikipedia + Maximum Fun transcripts), so there is no off-box
backup. Path is overridable with the ``JJHO_DB`` env var.

Schema (idempotent, versioned via a ``meta`` row):

- ``episodes`` — one row per feed item, keyed by the RSS ``guid`` (stable id).
  The spine: number, title, publish date, dispute blurb, audio + listen URLs,
  guest bailiff, and ``source`` flags recording which sources contributed.
- ``transcripts`` — one row per episode with a fetched transcript, FK to
  ``episodes(id)``.

All writes go through idempotent UPSERTs so re-running ingest is safe.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

# Repo-root by default. The data dir (SQLite index + scrape caches) is
# repo-root/data, overridable with JJHO_DATA (the container mounts a volume
# there). The DB file itself can be pointed elsewhere with JJHO_DB.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    env = os.environ.get("JJHO_DATA")
    return Path(env).expanduser() if env else (_REPO_ROOT / "data")


def db_path() -> Path:
    env = os.environ.get("JJHO_DB")
    return Path(env).expanduser() if env else (data_dir() / "jjho.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """Open the index, creating parent dirs and the schema if needed."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables + indexes if absent; record the schema version."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id             TEXT PRIMARY KEY,   -- RSS guid (stable id)
            number         INTEGER,            -- episode number (may be NULL: teasers/specials)
            title          TEXT NOT NULL,
            pub_date       TEXT,               -- ISO 8601 (UTC) or NULL
            pub_date_raw   TEXT,               -- original RSS pubDate string
            blurb          TEXT,               -- dispute blurb (RSS summary, else Wikipedia dispute)
            wiki_dispute   TEXT,               -- Wikipedia one-line dispute description
            audio_url      TEXT,               -- enclosure (mp3)
            listen_url     TEXT,               -- episode permalink
            guest_bailiff  TEXT,               -- from Wikipedia
            has_transcript INTEGER NOT NULL DEFAULT 0,
            from_rss       INTEGER NOT NULL DEFAULT 0,
            from_wikipedia INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_number   ON episodes(number);
        CREATE INDEX IF NOT EXISTS idx_episodes_pub_date ON episodes(pub_date);

        CREATE TABLE IF NOT EXISTS transcripts (
            episode_id     TEXT PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
            full_text      TEXT,
            source_url     TEXT,
            fetched_at     TEXT,
            has_transcript INTEGER NOT NULL DEFAULT 0,
            -- Provenance (schema v2): 'maxfun' = official human transcript,
            -- 'asr' = machine-generated (Whisper). ``asr_model`` records the
            -- model id for ASR rows (NULL for maxfun). See jjho/data/asr.py.
            source         TEXT NOT NULL DEFAULT 'maxfun',
            asr_model      TEXT
        );
        """
    )
    _migrate_transcript_provenance(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_transcript_provenance(conn: sqlite3.Connection) -> None:
    """Idempotent v1→v2 migration: add ``source``/``asr_model`` to a
    pre-existing ``transcripts`` table and backfill legacy rows to
    ``source='maxfun'``.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so each add is guarded by a
    ``PRAGMA table_info`` membership check. Fresh DBs already have the columns
    (the CREATE TABLE above), making the ADDs no-ops; only DBs created under
    schema v1 actually get altered. The backfill is a plain idempotent UPDATE
    (any row with a NULL ``source`` — i.e. an old MaxFun row — becomes
    ``'maxfun'``), safe to run on every open.
    """
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(transcripts)").fetchall()}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE transcripts ADD COLUMN source TEXT "
            "NOT NULL DEFAULT 'maxfun'")
    if "asr_model" not in cols:
        conn.execute("ALTER TABLE transcripts ADD COLUMN asr_model TEXT")
    # Backfill any legacy/NULL provenance to the official-transcript default.
    conn.execute(
        "UPDATE transcripts SET source = 'maxfun' WHERE source IS NULL")


def upsert_episode_rss(conn: sqlite3.Connection, ep: dict) -> str:
    """Insert/update an episode from the RSS spine, keyed by guid ``id``.

    Returns ``"inserted"`` or ``"updated"``. RSS is authoritative for
    title/date/audio/listen/blurb; Wikipedia-only fields are left untouched.
    """
    now = _now()
    cur = conn.execute("SELECT id FROM episodes WHERE id = ?", (ep["id"],))
    exists = cur.fetchone() is not None
    conn.execute(
        """
        INSERT INTO episodes (
            id, number, title, pub_date, pub_date_raw, blurb,
            audio_url, listen_url, from_rss, created_at, updated_at
        ) VALUES (
            :id, :number, :title, :pub_date, :pub_date_raw, :blurb,
            :audio_url, :listen_url, 1, :now, :now
        )
        ON CONFLICT(id) DO UPDATE SET
            number       = excluded.number,
            title        = excluded.title,
            pub_date     = excluded.pub_date,
            pub_date_raw = excluded.pub_date_raw,
            blurb        = excluded.blurb,
            audio_url    = excluded.audio_url,
            listen_url   = excluded.listen_url,
            from_rss     = 1,
            updated_at   = :now
        """,
        {**ep, "now": now},
    )
    return "inserted" if not exists else "updated"


def enrich_episode_wikipedia(
    conn: sqlite3.Connection, episode_id: str, guest_bailiff: str | None,
    dispute: str | None,
) -> None:
    """Apply Wikipedia enrichment to an already-matched episode row.

    Sets ``guest_bailiff`` and ``wiki_dispute``, marks ``from_wikipedia``, and
    back-fills ``blurb`` from the dispute only when the RSS blurb is empty.
    """
    now = _now()
    conn.execute(
        """
        UPDATE episodes SET
            guest_bailiff  = COALESCE(NULLIF(:guest, ''), guest_bailiff),
            wiki_dispute   = COALESCE(NULLIF(:dispute, ''), wiki_dispute),
            blurb          = CASE
                                WHEN blurb IS NULL OR blurb = ''
                                THEN NULLIF(:dispute, '') ELSE blurb
                             END,
            from_wikipedia = 1,
            updated_at     = :now
        WHERE id = :id
        """,
        {"id": episode_id, "guest": guest_bailiff or "",
         "dispute": dispute or "", "now": now},
    )


def set_has_transcript(conn: sqlite3.Connection, episode_id: str,
                       has: bool) -> None:
    conn.execute(
        "UPDATE episodes SET has_transcript = ?, updated_at = ? WHERE id = ?",
        (1 if has else 0, _now(), episode_id),
    )


def upsert_transcript(conn: sqlite3.Connection, episode_id: str,
                      full_text: str | None, source_url: str,
                      has_transcript: bool, *, source: str = "maxfun",
                      asr_model: str | None = None) -> None:
    """Idempotently store a transcript fetch result for an episode.

    ``source`` records provenance: ``'maxfun'`` (default) for official human
    transcripts scraped from Maximum Fun, ``'asr'`` for machine-generated
    Whisper transcripts (with ``asr_model`` set to the model id). The default
    keeps the existing MaxFun ingest path writing ``source='maxfun'`` unchanged.
    """
    conn.execute(
        """
        INSERT INTO transcripts (episode_id, full_text, source_url,
                                 fetched_at, has_transcript, source, asr_model)
        VALUES (:eid, :text, :url, :fetched, :has, :source, :asr_model)
        ON CONFLICT(episode_id) DO UPDATE SET
            full_text      = excluded.full_text,
            source_url     = excluded.source_url,
            fetched_at     = excluded.fetched_at,
            has_transcript = excluded.has_transcript,
            source         = excluded.source,
            asr_model      = excluded.asr_model
        """,
        {"eid": episode_id, "text": full_text, "url": source_url,
         "fetched": _now(), "has": 1 if has_transcript else 0,
         "source": source, "asr_model": asr_model},
    )
    set_has_transcript(conn, episode_id, has_transcript)


def list_episodes(conn: sqlite3.Connection, q: str | None = None) -> list[dict]:
    """Episodes for the browser, newest first.

    Optional ``q`` is a plain-text filter over title + blurb (used for the
    no-JS path; the page also filters client-side). Episodes with no publish
    date sort last.
    """
    sql = (
        "SELECT id, number, title, pub_date, pub_date_raw, blurb, "
        "       listen_url, audio_url, guest_bailiff, has_transcript "
        "FROM episodes "
    )
    params: tuple = ()
    if q:
        like = f"%{q.strip()}%"
        sql += ("WHERE title LIKE ? COLLATE NOCASE "
                "OR blurb LIKE ? COLLATE NOCASE ")
        params = (like, like)
    sql += "ORDER BY (pub_date IS NULL), pub_date DESC, number DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def coverage_stats(conn: sqlite3.Connection) -> dict:
    """Counts for the episode browser header + caveat."""
    total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    with_tx = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE has_transcript = 1").fetchone()[0]
    return {"total": total, "with_transcript": with_tx}


def episode_has_stored_transcript(conn: sqlite3.Connection,
                                  episode_id: str) -> bool:
    """True if we already fetched a transcript body for this episode.

    Provenance-agnostic: a non-empty transcript body of EITHER source
    (``maxfun`` or ``asr``) counts, so the ASR backfill skips anything already
    covered by an official transcript or a prior ASR run (resumable).
    """
    row = conn.execute(
        "SELECT has_transcript FROM transcripts "
        "WHERE episode_id = ? AND full_text IS NOT NULL AND full_text != ''",
        (episode_id,),
    ).fetchone()
    return bool(row and row["has_transcript"])


def episodes_needing_transcript(conn: sqlite3.Connection,
                                limit: int | None = None) -> list[dict]:
    """Numbered episodes with an ``audio_url`` but NO stored transcript body of
    either source — the ASR backfill work queue, newest-first.

    Newest-first (``number DESC``) so the most-listened recent gaps fill first.
    A LEFT JOIN excludes any episode that already has a non-empty transcript
    (maxfun or asr), making the batch resumable: re-running only picks up what
    is still missing.
    """
    sql = (
        "SELECT e.id, e.number, e.title, e.audio_url "
        "FROM episodes e "
        "LEFT JOIN transcripts t ON t.episode_id = e.id "
        "  AND t.full_text IS NOT NULL AND t.full_text != '' "
        "WHERE e.number IS NOT NULL "
        "  AND e.audio_url IS NOT NULL AND e.audio_url != '' "
        "  AND t.episode_id IS NULL "
        "ORDER BY e.number DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def transcript_counts_by_source(conn: sqlite3.Connection) -> dict:
    """``{source: count}`` over transcripts with a non-empty body, plus a
    ``total`` key — powers the ASR CLI's coverage summary."""
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM transcripts "
        "WHERE full_text IS NOT NULL AND full_text != '' "
        "GROUP BY source"
    ).fetchall()
    out = {r["source"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    return out


# ---------------------------------------------------------------------------
# Super Search read helpers
#
# These back the cost-tiered episode search (jjho/web/search.py):
#  - ``spine_for_search`` feeds the CHEAP tier: the whole episode spine
#    (number/title/blurb/dispute), one Claude call, no transcripts.
#  - ``transcripts_for_terms`` feeds the DEEP tier: a *bounded* keyword-LIKE
#    filter over ``transcripts.full_text`` for the query's salient terms, so
#    Claude only ever sees ~15-25 candidate episodes plus matched excerpts,
#    never every transcript at once.
# ---------------------------------------------------------------------------

def episode_count(conn: sqlite3.Connection) -> int:
    """Number of episodes in the index (0 = index not built yet)."""
    return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def spine_for_search(conn: sqlite3.Connection) -> list[dict]:
    """Every episode's searchable spine fields, for the cheap search tier.

    Bounded, cheap, complete — number/title/blurb/dispute for all ~760
    episodes plus the fields the result cards need (listen/audio URLs, guest
    bailiff, transcript flag, date). Ordered by episode number (specials last).
    """
    rows = conn.execute(
        "SELECT id, number, title, blurb, wiki_dispute, listen_url, "
        "       audio_url, guest_bailiff, has_transcript, pub_date, "
        "       pub_date_raw "
        "FROM episodes "
        "ORDER BY (number IS NULL), number"
    ).fetchall()
    return [dict(r) for r in rows]


def _extract_excerpts(text: str, terms: list[str], window: int = 140,
                      max_excerpts: int = 2) -> list[str]:
    """Pull up to ``max_excerpts`` whitespace-collapsed snippets around the
    first occurrences of ``terms`` in ``text``.

    Each excerpt is ~``2 * window`` chars centred on a matched term, with
    ellipses marking truncation. Excerpts that would overlap an already-chosen
    one are skipped so a single dense paragraph doesn't produce duplicates.
    Pure/deterministic — unit-tested independently of the DB.
    """
    if not text or not terms:
        return []
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for t in terms:
        idx = low.find(t)
        if idx != -1:
            hits.append((idx, t))
    hits.sort()
    excerpts: list[str] = []
    used: list[int] = []
    for idx, t in hits:
        if len(excerpts) >= max_excerpts:
            break
        if any(abs(idx - u) < window for u in used):
            continue
        start = max(0, idx - window)
        end = min(len(text), idx + len(t) + window)
        snippet = " ".join(text[start:end].split())
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        excerpts.append(f"{prefix}{snippet}{suffix}")
        used.append(idx)
    return excerpts


def transcripts_for_terms(conn: sqlite3.Connection, terms: list[str],
                          limit: int = 22, window: int = 140,
                          max_excerpts: int = 2) -> list[dict]:
    """Bounded deep-search candidate set: episodes whose stored transcript
    contains any of ``terms``, ranked by how many distinct terms matched (then
    by total occurrences), each carrying matched excerpts.

    Keyword-LIKE, not FTS: every term is a bound ``?`` parameter (only the
    *number* of OR clauses is interpolated), so there is no SQL-injection
    surface. Returns at most ``limit`` rows so the Claude prompt stays cheap.
    Empty ``terms`` -> no candidates.
    """
    terms = [t for t in terms if t]
    if not terms:
        return []
    like_clauses = " OR ".join(
        ["t.full_text LIKE ? COLLATE NOCASE"] * len(terms))
    params = [f"%{t}%" for t in terms]
    sql = (
        "SELECT e.id, e.number, e.title, e.blurb, e.wiki_dispute, "
        "       e.listen_url, e.audio_url, e.guest_bailiff, "
        "       e.has_transcript, e.pub_date, e.pub_date_raw, t.full_text "
        "FROM transcripts t JOIN episodes e ON e.id = t.episode_id "
        "WHERE t.full_text IS NOT NULL AND t.full_text != '' "
        f"AND ({like_clauses})"
    )
    scored: list[dict] = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        text = d.pop("full_text") or ""
        low = text.lower()
        d["match_terms"] = sum(1 for t in terms if t in low)
        d["match_total"] = sum(low.count(t) for t in terms)
        d["excerpts"] = _extract_excerpts(text, terms, window, max_excerpts)
        scored.append(d)
    scored.sort(key=lambda d: (d["match_terms"], d["match_total"]),
                reverse=True)
    return scored[:limit]
