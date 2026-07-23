"""Route-level behaviour for GET /search, incl. graceful degradation.

The Anthropic client is patched to a FakeClient — no real API calls. The route
opens its own DB connection to the tmp ``JJHO_DB`` file, so we seed through a
separate committed connection first.
"""

from __future__ import annotations

import pytest

from jjho.data import db
from jjho.web import search as search_engine
from jjho.web.app import create_app

from .conftest import FakeClient, json_matches, seed_episode


@pytest.fixture()
def client():
    return create_app().test_client()


def _seed(episodes_transcript=True):
    c = db.get_conn()
    try:
        seed_episode(c, id="a", number=1, title="Pop-Tart Sandwich",
                     blurb="Is a Pop-Tart a sandwich?",
                     transcript="pop tart sandwich pastry" if episodes_transcript else None)
        seed_episode(c, id="b", number=2, title="Toaster Trial", blurb="Toaster")
    finally:
        c.close()


def test_nav_has_super_search_link(client):
    resp = client.get("/search")
    assert resp.status_code == 200
    assert b"Super Search" in resp.data


def test_no_query_shows_hint(client):
    resp = client.get("/search")
    assert resp.status_code == 200
    assert b"most likely cases" in resp.data


def test_no_index_panel(client):
    # DB exists but is empty
    resp = client.get("/search?q=pop+tart")
    assert resp.status_code == 200
    assert b"Index not built yet" in resp.data


def test_no_api_key_panel(client):
    _seed()
    # No ANTHROPIC_API_KEY set (fixture cleared it) -> friendly panel, not 500
    resp = client.get("/search?q=pop+tart")
    assert resp.status_code == 200
    assert b"Super Search needs an API key" in resp.data


def test_cheap_results_and_escalation(client, monkeypatch):
    _seed()

    def script(model, kwargs):
        return json_matches([{"ref": 1, "reason": "the pop tart one",
                              "confidence": "high"}])

    monkeypatch.setattr(search_engine, "_get_client",
                        lambda: FakeClient(script))
    resp = client.get("/search?q=pop+tart+sandwich")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Pop-Tart Sandwich" in body
    assert "the pop tart one" in body
    assert "high confidence" in body
    # cheap search WITH results -> escalation control shown
    assert "Try Super Search" in body
    assert "deep=1" in body


def test_deep_hides_escalation(client, monkeypatch):
    _seed()

    def script(model, kwargs):
        return json_matches([{"ref": 1, "reason": "r", "confidence": "medium"}])

    monkeypatch.setattr(search_engine, "_get_client",
                        lambda: FakeClient(script))
    resp = client.get("/search?q=pop+tart&deep=1")
    assert resp.status_code == 200
    body = resp.data.decode()
    # deep already ran -> no escalation offer
    assert "Try Super Search" not in body
    assert "Searched full transcripts" in body


def test_no_match_message(client, monkeypatch):
    _seed()
    monkeypatch.setattr(search_engine, "_get_client",
                        lambda: FakeClient(lambda m, k: json_matches([])))
    resp = client.get("/search?q=nonsense")
    assert resp.status_code == 200
    assert b"No confident match" in resp.data
