"""Tests for the ASR (Whisper) transcript layer — no network, no model.

Every test monkeypatches :func:`jjho.data.asr._download_audio` and
:func:`jjho.data.asr._transcribe_file`, so nothing touches the network or loads
``mlx_whisper``/the model weights. Covers the work-queue selection, the
store-with-provenance path (incl. the sub-threshold sentinel), and the v1→v2
schema migration/backfill.
"""

from __future__ import annotations

from jjho.data import asr, db

from .conftest import seed_episode

LONG_TEXT = "This is a judged dispute. " * 60      # ~1560 chars (>= 800)
SHORT_TEXT = "Too short."                          # < 800 chars


# ---------------------------------------------------------------------------
# Selection / work queue
# ---------------------------------------------------------------------------

def test_selection_picks_missing_skips_covered(conn):
    """Only an episode with audio + no transcript body is queued; ones already
    covered by a maxfun OR an asr transcript are skipped."""
    seed_episode(conn, id="e-need", number=100, title="Needs ASR")
    seed_episode(conn, id="e-maxfun", number=101, title="Has MaxFun",
                 transcript="official transcript body")   # source=maxfun
    seed_episode(conn, id="e-asr", number=102, title="Has ASR")
    db.upsert_transcript(conn, "e-asr", "machine transcript body",
                         "https://x/e-asr.mp3", has_transcript=True,
                         source="asr", asr_model="m")
    conn.commit()

    ids = {e["id"] for e in db.episodes_needing_transcript(conn)}
    assert ids == {"e-need"}


def test_selection_newest_first(conn):
    """The queue is ordered newest (highest number) first."""
    for n in (50, 90, 70):
        seed_episode(conn, id=f"e{n}", number=n, title=f"ep {n}")
    nums = [e["number"] for e in db.episodes_needing_transcript(conn)]
    assert nums == [90, 70, 50]


def test_selection_requires_audio_url(conn):
    """An episode with no audio_url is never queued (nothing to transcribe)."""
    seed_episode(conn, id="e-audio", number=200, title="Has audio")
    seed_episode(conn, id="e-noaudio", number=201, title="No audio")
    conn.execute("UPDATE episodes SET audio_url = '' WHERE id = 'e-noaudio'")
    conn.commit()
    ids = {e["id"] for e in db.episodes_needing_transcript(conn)}
    assert ids == {"e-audio"}


# ---------------------------------------------------------------------------
# Storage / provenance
# ---------------------------------------------------------------------------

def _patch_pipeline(monkeypatch, text):
    """Make the download a no-op and the transcribe return canned ``text``."""
    monkeypatch.setattr(asr, "_download_audio",
                        lambda url, dest: (open(dest, "wb").close() or 0))
    monkeypatch.setattr(asr, "_transcribe_file", lambda path, model: text)
    monkeypatch.setattr(asr, "_probe_duration", lambda path: None)


def test_stores_asr_row_above_threshold(conn, monkeypatch):
    seed_episode(conn, id="e1", number=300, title="Transcribe me")
    _patch_pipeline(monkeypatch, LONG_TEXT)

    summary = asr.transcribe_missing(conn, model="test-model")
    assert summary == {"targeted": 1, "transcribed": 1, "short": 0, "failed": 0}

    row = conn.execute(
        "SELECT full_text, source, asr_model, has_transcript, source_url "
        "FROM transcripts WHERE episode_id = 'e1'").fetchone()
    assert row["source"] == "asr"
    assert row["asr_model"] == "test-model"
    assert row["has_transcript"] == 1
    assert row["full_text"] == LONG_TEXT
    assert row["source_url"] == "https://x/e1.mp3"      # the audio_url
    # And the episode flag is flipped too.
    assert conn.execute(
        "SELECT has_transcript FROM episodes WHERE id='e1'").fetchone()[0] == 1


def test_sub_threshold_marks_no_transcript(conn, monkeypatch):
    seed_episode(conn, id="e2", number=301, title="Barely anything")
    _patch_pipeline(monkeypatch, SHORT_TEXT)

    summary = asr.transcribe_missing(conn, model="test-model")
    assert summary == {"targeted": 1, "transcribed": 0, "short": 1, "failed": 0}

    row = conn.execute(
        "SELECT full_text, source, asr_model, has_transcript "
        "FROM transcripts WHERE episode_id = 'e2'").fetchone()
    assert row["has_transcript"] == 0
    assert row["full_text"] is None
    assert row["source"] == "asr"          # provenance still recorded
    assert row["asr_model"] == "test-model"


def test_failure_is_isolated_not_fatal(conn, monkeypatch):
    """A download/transcribe exception is tallied and skipped, not raised."""
    seed_episode(conn, id="e-fail", number=400, title="Boom")
    seed_episode(conn, id="e-ok", number=401, title="Fine")

    def boom(url, dest):
        raise RuntimeError("network go boom")

    # e-ok (number 401) runs first (newest-first); e-fail second.
    monkeypatch.setattr(asr, "_probe_duration", lambda path: None)
    monkeypatch.setattr(asr, "_transcribe_file", lambda p, m: LONG_TEXT)

    def dl(url, dest):
        if "e-fail" in url:
            raise RuntimeError("network go boom")
        open(dest, "wb").close()
        return 0
    monkeypatch.setattr(asr, "_download_audio", dl)

    summary = asr.transcribe_missing(conn)
    assert summary["failed"] == 1
    assert summary["transcribed"] == 1
    # The good one still stored; the failed one has no body.
    assert db.episode_has_stored_transcript(conn, "e-ok")
    assert not db.episode_has_stored_transcript(conn, "e-fail")


def test_resumable_skips_already_transcribed(conn, monkeypatch):
    """A second run does nothing once bodies exist (idempotent/resumable)."""
    seed_episode(conn, id="e3", number=500, title="Once")
    _patch_pipeline(monkeypatch, LONG_TEXT)
    asr.transcribe_missing(conn)
    second = asr.transcribe_missing(conn)
    assert second == {"targeted": 0, "transcribed": 0, "short": 0, "failed": 0}


# ---------------------------------------------------------------------------
# Schema migration / backfill
# ---------------------------------------------------------------------------

def test_fresh_db_has_provenance_columns(conn):
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(transcripts)").fetchall()}
    assert {"source", "asr_model"} <= cols


def test_maxfun_rows_default_to_maxfun(conn):
    seed_episode(conn, id="e-mf", number=600, title="Official",
                 transcript="a real official transcript body")
    row = conn.execute(
        "SELECT source, asr_model FROM transcripts "
        "WHERE episode_id = 'e-mf'").fetchone()
    assert row["source"] == "maxfun"
    assert row["asr_model"] is None


def test_migration_backfills_legacy_v1_db(tmp_path, monkeypatch):
    """A DB built under the old v1 schema (no source/asr_model) gains the
    columns and its legacy rows read back as source='maxfun'."""
    import sqlite3
    p = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(p))
    raw.executescript(
        """
        CREATE TABLE episodes (id TEXT PRIMARY KEY, number INTEGER,
            title TEXT NOT NULL, pub_date TEXT, pub_date_raw TEXT, blurb TEXT,
            wiki_dispute TEXT, audio_url TEXT, listen_url TEXT,
            guest_bailiff TEXT, has_transcript INTEGER NOT NULL DEFAULT 0,
            from_rss INTEGER NOT NULL DEFAULT 0,
            from_wikipedia INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE transcripts (episode_id TEXT PRIMARY KEY, full_text TEXT,
            source_url TEXT, fetched_at TEXT,
            has_transcript INTEGER NOT NULL DEFAULT 0);
        INSERT INTO episodes(id, number, title, audio_url, has_transcript,
            created_at, updated_at)
            VALUES('e', 1, 't', 'u', 1, 'n', 'n');
        INSERT INTO transcripts VALUES('e', 'legacy body', 'u', 'n', 1);
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("JJHO_DB", str(p))
    conn = db.get_conn()          # triggers init_schema -> migration
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(transcripts)").fetchall()}
        assert {"source", "asr_model"} <= cols
        row = conn.execute(
            "SELECT source, asr_model FROM transcripts "
            "WHERE episode_id='e'").fetchone()
        assert row["source"] == "maxfun"
        assert row["asr_model"] is None
    finally:
        conn.close()
