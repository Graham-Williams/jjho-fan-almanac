"""Flask application: routes + security middleware (SKELETON).

Security posture mirrors the sibling apps (taste-twin / todoist-points /
km-tracker); see the repo CLAUDE.md:

- App-level shared-password gate when APP_PASSWORD is set: every request that
  isn't /login, /logout, a static asset or /healthz is redirected to /login
  until the visitor presents the one shared password. Success stores a signed
  session marker (SESSION_SECRET); the raw password is never stored or logged.
  Unset APP_PASSWORD = gate OFF (local dev only — never expose unset).
- APP_HOST, when set, pins the Host (all requests) and Origin (POSTs) headers —
  CSRF/rebinding defense for state-changing routes incl. POST /login.
- Cloudflare Access JWT verification is DEFERRED (the shared-password gate is
  the active sign-in). The env vars CF_ACCESS_AUD / CF_ACCESS_TEAM_DOMAIN are
  documented in .env.example for a future identity-gated mode.

This module ships the booting skeleton only: a health route, a placeholder
home page, and the sign-in gate. The four features get their own routes on
feature branches — see DESIGN.md.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from datetime import datetime

from flask import (Flask, Response, abort, redirect, render_template, request,
                   session, url_for)

from ..data import db
from . import search as search_engine
from .password_gate import LoginRateLimiter, client_ip, safe_next


def _safe_url(u: str | None) -> str:
    """Only let http(s) URLs reach an href — blank a javascript:/data: scheme.

    Jinja autoescaping prevents attribute breakout but not a hostile scheme in
    a scraped URL, so gate the scheme before it lands in the template.
    """
    if not u:
        return ""
    return u if urlsplit(u).scheme in ("http", "https") else ""


def _fmt_date(pub_date: str | None, raw: str | None) -> str:
    """Human date for a row (ISO pref, else the raw RSS string, else blank)."""
    if pub_date:
        try:
            return datetime.fromisoformat(pub_date).strftime("%b %-d, %Y")
        except ValueError:
            pass
    return (raw or "").split("+")[0].strip()

log = logging.getLogger("jjho.web")

_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'")


def create_app() -> Flask:
    """App factory. Boots with the shared-password gate + placeholder home."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s",
                            datefmt="%H:%M:%S")

    app = Flask(__name__)
    app.jinja_env.filters["safe_url"] = _safe_url

    app_host = os.environ.get("APP_HOST", "")
    if not app_host:
        log.warning("APP_HOST not set — Host/Origin pinning disabled "
                    "(local dev mode only).")

    # -- app-level shared-password gate (env-gated by APP_PASSWORD) -----------
    app_password = os.environ.get("APP_PASSWORD", "")
    password_gate_enabled = bool(app_password)
    session_secret = (os.environ.get("SESSION_SECRET", "")
                      or os.environ.get("SECRET_KEY", ""))
    app.secret_key = session_secret or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    login_limiter = LoginRateLimiter()
    # Bound Super Search API cost: a light per-IP throttle on DEEP searches
    # (each is a Claude call over transcripts). Reuses the login limiter's
    # sliding-window; ~30 deep searches / 15 min / IP before a soft block.
    deep_search_limiter = LoginRateLimiter(max_failures=30, window_seconds=900)
    if password_gate_enabled:
        if not session_secret:
            log.warning("APP_PASSWORD set but SESSION_SECRET/SECRET_KEY unset "
                        "— using an ephemeral signing key; sessions won't "
                        "survive a restart. Set SESSION_SECRET.")
        log.info("APP_PASSWORD set — shared-password gate ENABLED.")
    else:
        log.warning("APP_PASSWORD unset — shared-password gate OFF "
                    "(local dev mode only; never expose this).")
    gate_exempt_paths = {"/login", "/logout", "/healthz"}

    # -- middleware -----------------------------------------------------------

    @app.before_request
    def _password_gate():  # runs first; redirects unauth users to /login
        if not password_gate_enabled:
            return None
        path = request.path
        if path in gate_exempt_paths:
            return None
        if (request.endpoint == "static"
                or path.startswith(app.static_url_path.rstrip("/") + "/")):
            return None
        if session.get("jjho_authed") is True:
            return None
        nxt = path
        if request.query_string:
            nxt = f"{path}?{request.query_string.decode('latin-1')}"
        return redirect(url_for("login", next=nxt))

    @app.before_request
    def _host_origin_pin():
        if not app_host or request.path == "/healthz":
            return None
        if request.host.split(":", 1)[0].lower() != app_host:
            abort(403)
        if request.method == "POST":
            # CSRF defense: a state-changing POST must carry at least one
            # same-origin signal that matches app_host. Origin is the most
            # trustworthy — if present it alone must match. When Origin is
            # absent (some browsers omit it) fall back to a same-host Referer.
            # A POST carrying NEITHER cannot be proven same-origin → rejected.
            origin = request.headers.get("Origin", "")
            referer = request.headers.get("Referer", "")
            if origin:
                if (urlsplit(origin).hostname or "").lower() != app_host:
                    abort(403)
            elif referer:
                if (urlsplit(referer).hostname or "").lower() != app_host:
                    abort(403)
            else:
                abort(403)
        return None

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        return resp

    # -- routes ---------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/login")
    def login():
        if not password_gate_enabled:
            return redirect(url_for("index"))
        if session.get("jjho_authed") is True:
            return redirect(safe_next(request.args.get("next")))
        return render_template(
            "login.html", next=request.args.get("next", ""), error=None)

    @app.post("/login")
    def login_post():
        # Host/Origin CSRF pin already ran in before_request for this POST.
        if not password_gate_enabled:
            return redirect(url_for("index"))
        next_target = request.form.get("next", "")
        ip = client_ip()
        if login_limiter.is_blocked(ip):
            log.warning("login blocked (rate limit) for %s", ip)
            return render_template(
                "login.html", next=next_target,
                error="Too many failed attempts. Try again in a few minutes."
            ), 429
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, app_password):
            session.clear()
            session["jjho_authed"] = True
            session.permanent = True
            login_limiter.reset(ip)
            return redirect(safe_next(next_target))
        login_limiter.record_failure(ip)
        log.warning("failed login attempt from %s", ip)  # never log the password
        return render_template(
            "login.html", next=next_target,
            error="Incorrect password."), 401

    @app.get("/logout")
    def logout():
        session.clear()
        if password_gate_enabled:
            return redirect(url_for("login"))
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/episodes")
    def episodes():
        q = (request.args.get("q") or "").strip()
        conn = db.get_conn()
        try:
            rows = db.list_episodes(conn, q or None)
            stats = db.coverage_stats(conn)
        finally:
            conn.close()
        for r in rows:
            r["date_display"] = _fmt_date(r.get("pub_date"),
                                          r.get("pub_date_raw"))
        return render_template("episodes.html", episodes=rows, q=q,
                               stats=stats)

    @app.get("/search")
    def search():
        """Super Search — read-only GET (shareable). ``deep=1`` = the deep,
        transcript-backed tier. Degrades gracefully (never 500)."""
        q = (request.args.get("q") or "").strip()
        deep = (request.args.get("deep") or "").lower() in (
            "1", "true", "on", "yes")

        result = None
        if q:
            # Soft per-IP throttle on the expensive deep tier only.
            if deep and deep_search_limiter.is_blocked(client_ip()):
                result = {"status": "rate_limited", "deep": True, "query": q,
                          "matches": []}
            else:
                if deep:
                    deep_search_limiter.record_failure(client_ip())
                conn = db.get_conn()
                try:
                    result = search_engine.run_search(conn, q, deep)
                finally:
                    conn.close()
                for m in result.get("matches", []):
                    m["date_display"] = _fmt_date(m.get("pub_date"),
                                                  m.get("pub_date_raw"))

        return render_template("search.html", q=q, deep=deep, result=result)

    return app
