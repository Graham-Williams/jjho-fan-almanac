"""Tier + escalation logic for run_search, with a MOCKED Anthropic client.

No test here makes a real API call — the client is always a FakeClient and we
assert on the calls it recorded.
"""

from __future__ import annotations

from jjho.web import search

from .conftest import FakeClient, json_matches, seed_episode


def _seed_three(conn):
    seed_episode(conn, id="a", number=1, title="Pop-Tart Sandwich",
                 blurb="Is a Pop-Tart a sandwich?",
                 transcript="the pop tart is basically a sandwich pastry")
    seed_episode(conn, id="b", number=2, title="Toaster Trial",
                 blurb="Toaster ownership",
                 transcript="the toaster and the pastry went to court")
    seed_episode(conn, id="c", number=3, title="Lawn Chairs", blurb="Chairs")


def test_empty_query(conn):
    r = search.run_search(conn, "   ", deep=False, client=FakeClient(lambda *a: ""))
    assert r["status"] == "empty_query"
    assert r["matches"] == []


def test_no_index(conn):
    # conn is a fresh empty DB
    r = search.run_search(conn, "anything", deep=False,
                          client=FakeClient(lambda *a: json_matches([])))
    assert r["status"] == "no_index"


def test_no_api_key(conn, monkeypatch):
    _seed_three(conn)
    # client=None and no ANTHROPIC_API_KEY -> degrade, no client construction
    r = search.run_search(conn, "pop tart", deep=False, client=None)
    assert r["status"] == "no_api_key"


def test_cheap_search_resolves_refs(conn):
    _seed_three(conn)

    def script(model, kwargs):
        # cheap tier: identify ref 1 (episode "a")
        return json_matches([{"ref": 1, "reason": "matches the pop tart bit",
                              "confidence": "high"}])

    client = FakeClient(script)
    r = search.run_search(conn, "pop tart sandwich", deep=False, client=client)
    assert r["status"] == "ok"
    assert [m["id"] for m in r["matches"]] == ["a"]
    assert r["matches"][0]["reason"] == "matches the pop tart bit"
    assert r["matches"][0]["confidence"] == "high"
    # exactly one Claude call for the cheap tier
    assert len(client.messages.calls) == 1
    assert "haiku" in client.messages.calls[0]["model"]


def test_error_status_on_client_exception(conn):
    _seed_three(conn)

    def boom(model, kwargs):
        raise RuntimeError("upstream 529")

    r = search.run_search(conn, "pop tart", deep=False, client=FakeClient(boom))
    assert r["status"] == "error"
    assert r["matches"] == []


def test_deep_is_superset_of_cheap(conn):
    """Deep must include cheap matches even if the deep model omits them."""
    _seed_three(conn)

    def script(model, kwargs):
        if "haiku" in model:            # cheap tier -> ref for episode "a"
            return json_matches([{"ref": 1, "reason": "cheap says A",
                                  "confidence": "medium"}])
        # deep tier -> picks a DIFFERENT episode (whatever ref maps to "b")
        prompt = kwargs["messages"][0]["content"]
        ref_b = _ref_for_title(prompt, "Toaster Trial")
        return json_matches([{"ref": ref_b, "reason": "deep says B",
                              "confidence": "high"}])

    client = FakeClient(script)
    r = search.run_search(conn, "toaster pastry court", deep=True, client=client)
    assert r["status"] == "ok"
    ids = [m["id"] for m in r["matches"]]
    assert "b" in ids and "a" in ids           # deep ⊇ cheap
    # two Claude calls: cheap then deep
    assert len(client.messages.calls) == 2
    assert "haiku" in client.messages.calls[0]["model"]
    assert "haiku" not in client.messages.calls[1]["model"]


def test_deep_thinking_disabled_for_deep_model(conn):
    _seed_three(conn)

    def script(model, kwargs):
        return json_matches([])

    client = FakeClient(script)
    search.run_search(conn, "toaster pastry", deep=True, client=client)
    deep_call = client.messages.calls[1]
    assert deep_call.get("thinking") == {"type": "disabled"}
    # cheap (haiku) call must NOT pass a thinking param
    assert "thinking" not in client.messages.calls[0]


def _ref_for_title(prompt: str, title: str) -> int:
    """Find the [ref] the deep prompt assigned to a given episode title."""
    import re
    for line in prompt.splitlines():
        m = re.match(r"\[(\d+)\][^:]*:\s*(.+)", line)
        if m and m.group(2).strip() == title:
            return int(m.group(1))
    raise AssertionError("title %r not in prompt" % title)
