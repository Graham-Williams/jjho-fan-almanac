"""Unit tests for the pure search helpers (no Claude, no Flask)."""

from __future__ import annotations

from jjho.data import db
from jjho.web import search

from .conftest import seed_episode


# ---- salient_terms ---------------------------------------------------------

def test_salient_terms_drops_stopwords_and_short():
    terms = search.salient_terms("the one about a Pop-Tart being a sandwich")
    assert "pop" in terms and "tart" in terms and "sandwich" in terms
    for junk in ("the", "one", "about", "a", "being"):
        assert junk not in terms


def test_salient_terms_dedupes_and_caps():
    terms = search.salient_terms("robot robot robot", max_terms=5)
    assert terms == ["robot"]
    many = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    assert len(search.salient_terms(many, max_terms=4)) == 4


def test_salient_terms_empty():
    assert search.salient_terms("") == []
    assert search.salient_terms("the a an of") == []


# ---- _extract_excerpts -----------------------------------------------------

def test_extract_excerpts_finds_term():
    text = ("Long before the dispute, " + ("filler " * 40)
            + "the defendant ate the entire toaster pastry in one bite "
            + ("more " * 40))
    ex = db._extract_excerpts(text, ["toaster"], window=40, max_excerpts=2)
    assert len(ex) == 1
    assert "toaster" in ex[0].lower()
    assert ex[0].startswith("…") and ex[0].endswith("…")


def test_extract_excerpts_none_when_absent():
    assert db._extract_excerpts("nothing here", ["missing"]) == []
    assert db._extract_excerpts("", ["x"]) == []


# ---- DB candidate retrieval ------------------------------------------------

def test_transcripts_for_terms_ranks_by_distinct_matches(conn):
    seed_episode(conn, id="a", number=1, title="Sandwich Law",
                 transcript="we argue whether a hot dog is a sandwich here")
    seed_episode(conn, id="b", number=2, title="Toaster Trial",
                 transcript="the sandwich and the toaster and the sandwich")
    seed_episode(conn, id="c", number=3, title="Unrelated",
                 transcript="a debate about lawn chairs and nothing else")

    rows = db.transcripts_for_terms(conn, ["sandwich", "toaster"])
    ids = [r["id"] for r in rows]
    # b matches both terms -> ranked first; c matches neither -> absent.
    assert ids[0] == "b"
    assert "c" not in ids
    assert rows[0]["match_terms"] == 2
    assert rows[0]["excerpts"]  # non-empty


def test_transcripts_for_terms_empty_terms(conn):
    seed_episode(conn, id="a", number=1, title="X", transcript="anything")
    assert db.transcripts_for_terms(conn, []) == []


def test_spine_and_count(conn):
    assert db.episode_count(conn) == 0
    seed_episode(conn, id="a", number=5, title="Five", blurb="b5")
    seed_episode(conn, id="b", number=None, title="Special")
    assert db.episode_count(conn) == 2
    spine = db.spine_for_search(conn)
    # numbered first, special (NULL number) last
    assert [e["id"] for e in spine] == ["a", "b"]


# ---- parse_matches ---------------------------------------------------------

def test_parse_matches_plain():
    out = search.parse_matches(
        '{"matches":[{"ref":3,"reason":"r","confidence":"high"}]}')
    assert out == [{"ref": 3, "reason": "r", "confidence": "high"}]


def test_parse_matches_salvages_from_prose():
    raw = 'Sure! Here is the answer:\n{"matches": [{"ref": 1, "confidence": "LOW"}]}\nHope that helps.'
    out = search.parse_matches(raw)
    assert out == [{"ref": 1, "reason": "", "confidence": "low"}]


def test_parse_matches_bare_list_and_bad_confidence():
    out = search.parse_matches('[{"ref":"2","reason":"x","confidence":"???"}]')
    assert out == [{"ref": 2, "reason": "x", "confidence": "medium"}]


def test_parse_matches_junk_returns_empty():
    assert search.parse_matches("not json at all") == []
    assert search.parse_matches("") == []
