"""Honest transcript-provenance labeling in the UI.

A machine-generated (Whisper/ASR) transcript must never be mistaken for an
official human one. These tests assert:

- the read helpers thread ``transcript_source`` / ``asr_model`` from the DB, and
- the two user-facing surfaces (The Docket + Super Search results) render the
  muted "🤖 Auto-generated" badge for ASR-backed episodes, the subtle
  "✓ Official transcript" marker for MaxFun-backed ones, and the plain
  "No transcript" marker when none is on file — with the accessible tooltip.

The Anthropic client is patched to a FakeClient (no real API calls).
"""

from __future__ import annotations

import pytest

from jjho.data import db
from jjho.web import search as search_engine
from jjho.web.app import create_app

from .conftest import FakeClient, json_matches, seed_episode

WHISPER = "mlx-community/whisper-large-v3-turbo"
# Match the rendered badge markup, not the bare words (the Docket's coverage
# legend also mentions "Auto-generated" / "Official" in prose).
AUTO_BADGE = 'class="pill asr"'
OFFICIAL_BADGE = 'class="pill official"'
ASR_TOOLTIP = "Machine-transcribed with Whisper; may contain errors."


@pytest.fixture()
def client():
    return create_app().test_client()


# ---------------------------------------------------------------------------
# Read helpers thread provenance
# ---------------------------------------------------------------------------

def test_list_episodes_threads_source(conn):
    seed_episode(conn, id="mf", number=10, title="Official one",
                 transcript="official body", source="maxfun")
    seed_episode(conn, id="ai", number=11, title="Machine one",
                 transcript="machine body", source="asr", asr_model=WHISPER)
    seed_episode(conn, id="none", number=12, title="No transcript one")

    rows = {r["id"]: r for r in db.list_episodes(conn)}
    assert rows["mf"]["transcript_source"] == "maxfun"
    assert rows["mf"]["asr_model"] is None
    assert rows["ai"]["transcript_source"] == "asr"
    assert rows["ai"]["asr_model"] == WHISPER
    # No transcript body -> no joined provenance (badge falls back to "none").
    assert rows["none"]["transcript_source"] is None
    assert rows["none"]["has_transcript"] == 0


def test_spine_for_search_threads_source(conn):
    seed_episode(conn, id="ai", number=20, title="Machine",
                 transcript="machine body", source="asr", asr_model=WHISPER)
    spine = {e["id"]: e for e in db.spine_for_search(conn)}
    assert spine["ai"]["transcript_source"] == "asr"
    assert spine["ai"]["asr_model"] == WHISPER


def test_transcripts_for_terms_threads_source(conn):
    seed_episode(conn, id="ai", number=30, title="Sandwich",
                 transcript="the sandwich dispute body", source="asr",
                 asr_model=WHISPER)
    rows = db.transcripts_for_terms(conn, ["sandwich"])
    assert rows and rows[0]["transcript_source"] == "asr"
    assert rows[0]["asr_model"] == WHISPER


# ---------------------------------------------------------------------------
# The Docket (episode browser) renders the badge
# ---------------------------------------------------------------------------

def _seed_docket():
    c = db.get_conn()
    try:
        seed_episode(c, id="mf", number=101, title="Official Episode",
                     blurb="Official blurb", transcript="official body",
                     source="maxfun")
        seed_episode(c, id="ai", number=102, title="Machine Episode",
                     blurb="Machine blurb", transcript="machine body",
                     source="asr", asr_model=WHISPER)
        seed_episode(c, id="none", number=103, title="Bare Episode",
                     blurb="No transcript blurb")
    finally:
        c.close()


def test_docket_shows_official_and_asr_badges(client):
    _seed_docket()
    body = client.get("/episodes").get_data(as_text=True)
    assert OFFICIAL_BADGE in body            # maxfun-backed
    assert AUTO_BADGE in body                # asr-backed
    assert ASR_TOOLTIP in body               # accessible tooltip present
    # The bare episode still shows the coverage-gap marker.
    assert "No transcript" in body


def test_docket_asr_badge_not_shown_when_only_official(client):
    c = db.get_conn()
    try:
        seed_episode(c, id="mf", number=201, title="Only Official",
                     transcript="official body", source="maxfun")
    finally:
        c.close()
    body = client.get("/episodes").get_data(as_text=True)
    assert OFFICIAL_BADGE in body
    assert AUTO_BADGE not in body


# ---------------------------------------------------------------------------
# Super Search results render the badge
# ---------------------------------------------------------------------------

def _seed_search(source, asr_model=None):
    c = db.get_conn()
    try:
        seed_episode(c, id="a", number=1, title="Pop-Tart Sandwich",
                     blurb="Is a Pop-Tart a sandwich?",
                     transcript="pop tart sandwich pastry body",
                     source=source, asr_model=asr_model)
    finally:
        c.close()


def _fake_first_match(monkeypatch):
    fake = FakeClient(lambda m, k: json_matches(
        [{"ref": 1, "reason": "the pop tart one", "confidence": "high"}]))
    monkeypatch.setattr(search_engine, "_get_client", lambda: fake)


def test_search_result_backed_by_asr_shows_auto_badge(client, monkeypatch):
    _seed_search("asr", WHISPER)
    _fake_first_match(monkeypatch)
    body = client.get("/search?q=pop+tart+sandwich&deep=1").get_data(as_text=True)
    assert "Pop-Tart Sandwich" in body
    assert AUTO_BADGE in body
    assert ASR_TOOLTIP in body
    assert OFFICIAL_BADGE not in body


def test_search_result_backed_by_maxfun_shows_official_marker(client, monkeypatch):
    _seed_search("maxfun")
    _fake_first_match(monkeypatch)
    body = client.get("/search?q=pop+tart+sandwich&deep=1").get_data(as_text=True)
    assert "Pop-Tart Sandwich" in body
    assert OFFICIAL_BADGE in body
    assert AUTO_BADGE not in body


def test_cheap_search_result_also_shows_provenance(client, monkeypatch):
    # The cheap tier reads the spine (which now carries provenance), so a cheap
    # hit for an ASR-backed episode also gets the auto-generated badge.
    _seed_search("asr", WHISPER)
    _fake_first_match(monkeypatch)
    body = client.get("/search?q=pop+tart+sandwich").get_data(as_text=True)
    assert AUTO_BADGE in body
