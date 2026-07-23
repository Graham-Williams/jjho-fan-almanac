"""Light guard for the responsive/mobile pass.

Responsive layout itself isn't unit-testable, but these cheap invariants keep a
future edit from silently dropping the viewport meta or the mobile media query
that make the app phone-first. Every page extends ``base.html``, so a route that
needs no DB/API key (``/search`` with no query renders the hint) is enough.
"""

from __future__ import annotations

from jjho.web.app import create_app


def _client():
    return create_app().test_client()


def test_viewport_meta_present():
    resp = _client().get("/search")
    assert resp.status_code == 200
    assert b'name="viewport"' in resp.data
    assert b"width=device-width" in resp.data


def test_base_ships_a_mobile_media_query():
    resp = _client().get("/search")
    body = resp.data.decode("utf-8")
    # The shared phone-first block lives in base.html's <style>.
    assert "@media (max-width: 640px)" in body


def test_inputs_have_no_ios_zoom_font_size():
    # Inputs must stay >= 1rem (16px) so iOS doesn't zoom the page on focus.
    body = _client().get("/search").get_data(as_text=True)
    assert "font-size: 1rem" in body  # from base input rule
