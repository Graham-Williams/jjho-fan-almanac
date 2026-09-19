"""Origin-side HTTPS enforcement (issue #17).

The edge already 301s http→https for every hostname, but that is one
Cloudflare dashboard toggle away from regressing, so the app enforces it
itself. These are the regression tests for the three load-bearing rules:

1. Redirect **only** when ``X-Forwarded-Proto``, trimmed and case-folded, is
   exactly ``http`` — the header rule IS the exemption list, so the in-network
   healthcheck (which sends no such header) must never be redirected. Schemes
   are case-insensitive (RFC 3986/9110), so ``HTTP`` must redirect too; a
   multi-hop ``http, https`` must not.
2. The target is built from the configured ``APP_HOST`` pin, **never** from
   the request's own Host header (that would be an open redirect), and the
   path + query survive byte-for-byte including percent-encoding.
3. ``Strict-Transport-Security: max-age=31536000`` on responses — no
   ``includeSubDomains``, no ``preload``.
4. The redirect is a **307** (method-preserving, not heuristically cacheable)
   carrying ``Cache-Control: no-store`` + ``Vary: X-Forwarded-Proto``.
"""

from __future__ import annotations

import pytest

from jjho.web.app import create_app

HOST = "jjho.graham-williams.com"
HTTP = {"X-Forwarded-Proto": "http"}
HTTPS = {"X-Forwarded-Proto": "https"}


@pytest.fixture()
def client(monkeypatch):
    """App with the APP_HOST pin set — i.e. the deployed configuration."""
    monkeypatch.setenv("APP_HOST", HOST)
    app = create_app()
    return app.test_client()


def _get(client, path, headers=None, host=HOST):
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", host)
    return client.get(path, headers=hdrs)


# ---- 1. the redirect fires only on X-Forwarded-Proto: http -----------------

def test_xfp_http_redirects_307_to_https(client):
    resp = _get(client, "/episodes", HTTP)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/episodes"


def test_xfp_https_is_not_redirected(client):
    resp = _get(client, "/healthz", HTTPS)
    assert resp.status_code == 200


def test_no_xfp_header_is_not_redirected(client):
    """The compose healthcheck urlopen()s http://127.0.0.1:8080/healthz with no
    X-Forwarded-Proto. A blanket "scheme is http" rule would redirect it and
    mark the container unhealthy forever."""
    resp = client.get("/healthz")  # no Host pin either — /healthz is pin-exempt
    assert resp.status_code == 200
    assert resp.json == {"status": "ok"}


def test_empty_xfp_header_is_not_redirected(client):
    resp = _get(client, "/healthz", {"X-Forwarded-Proto": ""})
    assert resp.status_code == 200


@pytest.mark.parametrize("value", ["http, https", "https, http", "HTTP, HTTPS"])
def test_xfp_list_value_is_not_redirected(client, value):
    """Only an unambiguous single "http" redirects; a chained-proxy list value
    fails open — we refuse to guess which hop the visitor was actually on.
    Normalising case must NOT accidentally start matching these."""
    resp = _get(client, "/healthz", {"X-Forwarded-Proto": value})
    assert resp.status_code == 200
    assert "Location" not in resp.headers


@pytest.mark.parametrize("value", ["http", "HTTP", "Http", "hTTp", " http",
                                   "http ", "  HTTP\t"])
def test_xfp_http_is_matched_case_insensitively_and_trimmed(client, value):
    """URI schemes are case-INSENSITIVE (RFC 3986 §3.1, RFC 9110).

    The original comparison was case-sensitive and failed in the *dangerous*
    direction: measured live against gunicorn, ``X-Forwarded-Proto: HTTP`` was
    served 200 over plain http with no upgrade at all.
    """
    resp = _get(client, "/episodes", {"X-Forwarded-Proto": value})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/episodes"


@pytest.mark.parametrize("value", ["httpx", "xhttp", "ws", " ", "http\x00"])
def test_near_miss_values_do_not_redirect(client, value):
    resp = _get(client, "/healthz", {"X-Forwarded-Proto": value})
    assert resp.status_code == 200


# ---- the redirect must not be cacheable ------------------------------------

def test_redirect_is_not_cacheable_and_declares_its_vary(client):
    """The Location is byte-identical to the request URL, so a cacheable
    redirect is a trap: a shared cache (Cloudflare caches .css/.js by default)
    could store it and replay it to https visitors — broken assets, or a loop.
    The response depends on X-Forwarded-Proto, so it must say so."""
    resp = _get(client, "/static/css/app.css", HTTP)
    assert resp.status_code == 307
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Vary"] == "X-Forwarded-Proto"


def test_redirect_preserves_the_method(client):
    """307, not 301: a plain-http POST is re-sent over https instead of being
    silently downgraded to a bodiless GET."""
    resp = client.post("/login", data={"password": "x"},
                       headers={**HTTP, "Host": HOST})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/login"


def test_redirect_beats_the_password_gate(monkeypatch):
    """The redirect must run BEFORE the gate — otherwise a plain-http visitor
    is served the login page (and would post the shared password) in the clear
    before ever being told to use https."""
    monkeypatch.setenv("APP_HOST", HOST)
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    c = create_app().test_client()
    resp = _get(c, "/episodes", HTTP)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/episodes"


def test_hook_registration_order_is_load_bearing(monkeypatch):
    """The whole security argument rests on _https_redirect being registered
    ahead of _password_gate (Flask runs before_request hooks in registration
    order). Pin it structurally so a reorder fails loudly, not silently."""
    monkeypatch.setenv("APP_HOST", HOST)
    names = [f.__name__ for f in create_app().before_request_funcs[None]]
    assert names[0] == "_https_redirect", names
    assert names.index("_https_redirect") < names.index("_password_gate")


# ---- 2. the target is pinned, and preserved byte-for-byte -----------------

def test_query_string_is_preserved(client):
    resp = _get(client, "/search?q=pop+tart&deep=1", HTTP)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/search?q=pop+tart&deep=1"


def test_percent_encoded_path_and_query_survive_exactly(client):
    """request.full_path would mangle this: Flask decodes request.path, so
    "/caf%C3%A9/a%20b" becomes "/café/a b" and the escaped "&" in the query
    ("%26") would be reproduced literally, splitting the parameter."""
    target = "/caf%C3%A9/a%20b?q=x%20y%26z&n=100%25"
    resp = _get(client, target, HTTP)
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}{target}"


def test_encoded_slash_in_path_is_not_decoded(client):
    resp = _get(client, "/a%2Fb", HTTP)
    assert resp.headers["Location"] == f"https://{HOST}/a%2Fb"


def test_target_survives_without_raw_uri(client):
    """Fallback path: a WSGI server that exposes neither RAW_URI nor
    REQUEST_URI still gets a correctly re-encoded target."""
    resp = client.get("/caf%C3%A9/a%20b?q=x%20y", headers={**HTTP, "Host": HOST},
                      environ_overrides={"RAW_URI": None, "REQUEST_URI": None})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/caf%C3%A9/a%20b?q=x%20y"


def test_crafted_host_header_is_not_reflected(client):
    """Open-redirect guard: the Location comes from APP_HOST, never the
    attacker-supplied Host header."""
    resp = _get(client, "/episodes?q=x", HTTP, host="evil.example.com")
    assert resp.status_code == 307
    loc = resp.headers["Location"]
    assert loc == f"https://{HOST}/episodes?q=x"
    assert "evil.example.com" not in loc


def test_protocol_relative_target_is_not_reflected(client):
    """A "//evil" request target must not produce a Location whose authority a
    browser could read as another site. (Set via the environ because the test
    client itself re-parses a "//host/path" argument as an absolute URL.)"""
    resp = client.get("/", headers={**HTTP, "Host": HOST},
                      environ_overrides={"RAW_URI": "//evil.example.com/x"})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/"


def test_backslash_target_is_not_reflected(client):
    resp = client.get("/", headers={**HTTP, "Host": HOST},
                      environ_overrides={"RAW_URI": "/\\evil.example.com/x"})
    assert resp.headers["Location"] == f"https://{HOST}/"


def test_control_character_in_raw_uri_cannot_split_the_header(client):
    resp = client.get("/", headers={**HTTP, "Host": HOST},
                      environ_overrides={"RAW_URI": "/x\r\nSet-Cookie: a=b"})
    assert resp.status_code == 307
    assert resp.headers["Location"] == f"https://{HOST}/"
    assert "Set-Cookie" not in resp.headers


# ---- APP_HOST unset / malformed => fail OPEN (local dev + tests still work) --

def test_no_app_host_means_no_redirect():
    c = create_app().test_client()  # APP_HOST cleared by the autouse fixture
    resp = c.get("/healthz", headers=HTTP)
    assert resp.status_code == 200


def test_malformed_app_host_disables_the_redirect(monkeypatch):
    monkeypatch.setenv("APP_HOST", "https://evil.example.com/x")
    c = create_app().test_client()
    resp = c.get("/healthz", headers=HTTP)
    assert resp.status_code == 200


# ---- 3. HSTS ---------------------------------------------------------------

def test_hsts_header_exact_value(client):
    resp = _get(client, "/", HTTPS)
    assert resp.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_hsts_has_no_subdomains_or_preload(client):
    """Each hostname owns its own policy — includeSubDomains here would commit
    every sibling app on graham-williams.com, and preload is irreversible."""
    value = _get(client, "/", HTTPS).headers["Strict-Transport-Security"]
    assert "includeSubDomains" not in value
    assert "preload" not in value


def test_hsts_on_healthz_and_login(client):
    for path in ("/healthz", "/login"):
        resp = _get(client, path, HTTPS)
        assert resp.headers.get("Strict-Transport-Security") == (
            "max-age=31536000"), path


def test_csp_is_unchanged_by_this_feature(client):
    """Issue #16 (the Cloudflare Web Analytics beacon) owns the CSP — this
    feature must not touch it."""
    csp = _get(client, "/", HTTPS).headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self'")
    assert "cloudflareinsights" not in csp


# ---- session cookie flags --------------------------------------------------

def test_session_cookie_is_secure_httponly_samesite(monkeypatch):
    monkeypatch.setenv("APP_HOST", HOST)
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    app = create_app()
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    c = app.test_client()
    resp = c.post("/login", data={"password": "hunter2", "next": "/episodes"},
                  headers={"Host": HOST, "Origin": f"https://{HOST}",
                           "X-Forwarded-Proto": "https"},
                  base_url=f"https://{HOST}")
    assert resp.status_code == 302
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    # Only a signed marker is stored — never the password itself.
    assert "hunter2" not in cookie
