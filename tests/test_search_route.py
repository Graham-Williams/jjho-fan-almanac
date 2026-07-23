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


def test_cheap_search_throttled_after_budget(monkeypatch):
    # Tiny overall budget so we don't have to fire 60 requests. Cheap-tier
    # searches must be metered too (they each make a paid Claude call) — the
    # finding was that only DEEP was throttled.
    monkeypatch.setenv("JJHO_SEARCH_MAX", "2")
    app = create_app()
    client = app.test_client()
    _seed()

    fake = FakeClient(lambda m, k: json_matches(
        [{"ref": 1, "reason": "the pop tart one", "confidence": "high"}]))
    monkeypatch.setattr(search_engine, "_get_client", lambda: fake)

    # The first two cheap searches run and each calls Claude once.
    for _ in range(2):
        resp = client.get("/search?q=pop+tart+sandwich")
        assert resp.status_code == 200
        assert b"Pop-Tart Sandwich" in resp.data
    assert len(fake.messages.calls) == 2

    # The third is throttled: friendly limited panel (not a 500), 429 status,
    # and — critically — NO additional Claude call is made.
    resp = client.get("/search?q=pop+tart+sandwich")
    assert resp.status_code == 429
    assert b"Easy there, counselor" in resp.data
    assert b"Too many searches" in resp.data
    assert len(fake.messages.calls) == 2  # unchanged: Claude was not called


def test_deep_consumes_overall_budget(monkeypatch):
    # A deep search counts against the overall budget too, so once the overall
    # budget is spent even a deep search is blocked (deep consumes both).
    monkeypatch.setenv("JJHO_SEARCH_MAX", "1")
    monkeypatch.setenv("JJHO_DEEP_SEARCH_MAX", "5")
    app = create_app()
    client = app.test_client()
    _seed()

    fake = FakeClient(lambda m, k: json_matches(
        [{"ref": 1, "reason": "r", "confidence": "medium"}]))
    monkeypatch.setattr(search_engine, "_get_client", lambda: fake)

    # One cheap search spends the whole overall budget of 1.
    resp = client.get("/search?q=pop+tart+sandwich")
    assert resp.status_code == 200
    calls_after_first = len(fake.messages.calls)
    assert calls_after_first >= 1

    # A deep search is now blocked by the overall budget despite deep budget
    # room — and it makes no further Claude call.
    resp = client.get("/search?q=pop+tart+sandwich&deep=1")
    assert resp.status_code == 429
    assert b"Easy there, counselor" in resp.data
    assert len(fake.messages.calls) == calls_after_first


def test_unmetered_degradation_does_not_charge_budget(monkeypatch):
    # A no-API-key search never calls Claude, so it must NOT consume the
    # budget: many such requests still leave a real search possible.
    monkeypatch.setenv("JJHO_SEARCH_MAX", "1")
    app = create_app()
    client = app.test_client()
    _seed()  # index present, but no ANTHROPIC_API_KEY -> no_api_key path

    for _ in range(3):
        resp = client.get("/search?q=pop+tart+sandwich")
        assert resp.status_code == 200
        assert b"Super Search needs an API key" in resp.data

    # Budget untouched: a request that WOULD call Claude still runs.
    fake = FakeClient(lambda m, k: json_matches(
        [{"ref": 1, "reason": "x", "confidence": "high"}]))
    monkeypatch.setattr(search_engine, "_get_client", lambda: fake)
    resp = client.get("/search?q=pop+tart+sandwich")
    assert resp.status_code == 200
    assert b"Pop-Tart Sandwich" in resp.data
    assert len(fake.messages.calls) == 1
