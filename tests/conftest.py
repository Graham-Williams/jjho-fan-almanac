"""Shared test fixtures for the Super Search suite.

Every test runs against a throwaway on-disk SQLite index (``JJHO_DB`` pointed
at a tmp file) and with the sign-in gate + Anthropic key cleared unless a test
sets them. The Anthropic client is always a fake — **no test makes a real API
call**.
"""

from __future__ import annotations

import json
import types

import pytest

from jjho.data import db


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Isolate every test: tmp DB, gate off, no API key, default models."""
    monkeypatch.setenv("JJHO_DB", str(tmp_path / "jjho.test.db"))
    monkeypatch.delenv("JJHO_DATA", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("JJHO_SEARCH_MODEL_CHEAP", raising=False)
    monkeypatch.delenv("JJHO_SEARCH_MODEL_DEEP", raising=False)
    yield


@pytest.fixture()
def conn():
    c = db.get_conn()
    yield c
    c.close()


def seed_episode(conn, *, id, number, title, blurb="", dispute=None,
                 transcript=None):
    """Insert one episode (and optionally its transcript) via the real UPSERTs."""
    db.upsert_episode_rss(conn, {
        "id": id, "number": number, "title": title,
        "pub_date": "2020-01-01T00:00:00+00:00", "pub_date_raw": "",
        "blurb": blurb, "audio_url": "https://x/%s.mp3" % id,
        "listen_url": "https://maximumfun.org/%s" % id,
    })
    if dispute is not None:
        db.enrich_episode_wikipedia(conn, id, None, dispute)
    if transcript is not None:
        db.upsert_transcript(conn, id, transcript,
                             "https://maximumfun.org/t/%s" % id, True)
    conn.commit()


# ---- fake Anthropic client -------------------------------------------------

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeMessages:
    def __init__(self, script):
        self._script = script
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._script(kwargs["model"], kwargs)
        return _Resp(text)


class FakeClient:
    """Records calls; returns whatever ``script(model, kwargs)`` produces."""

    def __init__(self, script):
        self.messages = FakeMessages(script)


def json_matches(matches):
    """Serialize a matches list the way the model would."""
    return json.dumps({"matches": matches})
