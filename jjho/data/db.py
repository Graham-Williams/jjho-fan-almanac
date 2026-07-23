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

SCHEMA_VERSION = 1

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
            has_transcript INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


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
                      has_transcript: bool) -> None:
    """Idempotently store a transcript fetch result for an episode."""
    conn.execute(
        """
        INSERT INTO transcripts (episode_id, full_text, source_url,
                                 fetched_at, has_transcript)
        VALUES (:eid, :text, :url, :fetched, :has)
        ON CONFLICT(episode_id) DO UPDATE SET
            full_text      = excluded.full_text,
            source_url     = excluded.source_url,
            fetched_at     = excluded.fetched_at,
            has_transcript = excluded.has_transcript
        """,
        {"eid": episode_id, "text": full_text, "url": source_url,
         "fetched": _now(), "has": 1 if has_transcript else 0},
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
    """True if we already fetched a transcript body for this episode."""
    row = conn.execute(
        "SELECT has_transcript FROM transcripts "
        "WHERE episode_id = ? AND full_text IS NOT NULL AND full_text != ''",
        (episode_id,),
    ).fetchone()
    return bool(row and row["has_transcript"])
