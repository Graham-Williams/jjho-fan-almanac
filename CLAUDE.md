# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**JJHo — The Fan Almanac**: an unofficial fan companion for the *Judge John
Hodgman* podcast (Maximum Fun; ~760 episodes). A courtroom-themed reference to
the disputes, precedents, running bits, and (eventually) verdicts. See
`README.md` for the user-facing overview and `DESIGN.md` for the full
architecture, data caveats, and phased build order.

**Not affiliated with Judge John Hodgman, John Hodgman, or Maximum Fun.** It is
built entirely from public data.

## Status

**Foundation built (Phase 1).** On top of the courtroom skeleton (home,
`/healthz`, shared-password gate) the **data spine + episode browser** are live:

- **Ingest pipeline** (`jjho/data/`, CLI `python -m jjho.data.ingest`) builds a
  gitignored SQLite index from the podcast RSS feed enriched with Wikipedia
  episode tables, and politely scrapes Maximum Fun transcripts into a transcript
  store. Idempotent + resumable.
- **The Docket** (`/episodes`) — a searchable, newest-first episode browser with
  an instant title/dispute filter and a per-episode transcript-on-file
  indicator (+ the coverage caveat in fine print).
- **Super Search** (`/search`) — cost-tiered, Claude-powered natural-language
  episode identification (see the *Super Search* section below).

Measured on the real data: **819 feed items** (784 numbered episodes),
**521 enriched from Wikipedia** (matched by title). Transcript sample: the
25 most-recent episodes are **4/25** covered (the newest ~14 have no transcript
yet — production lag); over the 100 most-recent it is **51/100**, and of the
episodes old enough to be transcribed (≤ ep 768) roughly **59%**.

The other three features (the Book of Settled Law, Motifs & Running Bits,
Justice Statistics) are not built yet; they land on feature branches per the
phased plan in `DESIGN.md`.

**Responsive/mobile pass done** (issue #8): the app is phone-first without
regressing desktop or changing the courtroom look. One shared
`@media (max-width: 640px)` block in `base.html` (full-width inputs, ≥44px tap
targets, wrapping flex nav, trimmed padding, `-webkit-text-size-adjust`) plus
per-page tweaks in `episodes.html`/`search.html`/`login.html`. No hamburger —
the 3-link nav just wraps (brand on its own line on mobile). Inputs stay ≥1rem
so iOS doesn't zoom on focus. No new deps, no inline JS (CSP). Media-query-scoped
so desktop CSS is untouched.

## Super Search (Phase 2)

Cost-tiered, Claude-powered episode identification. Read-only **`GET /search`**
(shareable; `?q=…&deep=1`). Renders `web/templates/search.html` (courtroom
aesthetic, reuses the Docket card look); nav link in `base.html`. Engine lives
in `web/search.py`; DB read helpers in `data/db.py`; the route is in `app.py`.

- **Cheap tier (default):** ONE Claude call over the episode **spine** (every
  episode's number/title/blurb/dispute — `db.spine_for_search`). Returns 1-3
  matches, each with a one-line reason + confidence. Model: **Haiku 4.5**
  (`claude-haiku-4-5`), env-overridable via `JJHO_SEARCH_MODEL_CHEAP`.
- **Deep tier ("Super Search" checkbox / escalation):** a **bounded** candidate
  set THEN Claude. `db.transcripts_for_terms` runs a keyword-LIKE filter over
  `transcripts.full_text` for the query's salient terms → top ~22 candidate
  episodes with matched **excerpts** (never full transcripts). Titles + excerpts
  go to **Sonnet 5** (`claude-sonnet-5`, env `JJHO_SEARCH_MODEL_DEEP`; thinking
  disabled to protect the JSON budget + keep it snappy). The cheap spine matches
  are unioned in so **deep ⊇ cheap**. **Deep-candidate mechanism = keyword-LIKE**
  (not FTS5): zero schema migration, works on the existing DB, coverage is small.
- **UX:** search box + a "Super Search" checkbox (deep up front). After a *cheap*
  search **with** results, a "Didn't find what you're looking for? Try Super
  Search" control links to `?q=<same>&deep=1` (hidden once deep has run). The
  transcript coverage caveat is in fine print by the controls.
- **Graceful degradation (never 500):** no `ANTHROPIC_API_KEY` (or `anthropic`
  not importable) → "needs an API key" panel; empty index → "index not built
  yet"; blank query → hint; any Claude/parse failure → friendly error panel.
  `run_search()` never raises and never logs the prompt body or the key.
- **Cost guard:** **every** search that reaches Claude is metered per-IP —
  cheap (one Haiku call over the spine) counts too, not just deep (the shared
  password means a leaked session could otherwise script `/search?q=…` and run
  up the Anthropic bill). Two sliding-window limiters (reusing
  `LoginRateLimiter`): an **overall** budget every Claude-calling search
  consumes (`search_limiter`, `JJHO_SEARCH_MAX`, default 60 / window) **plus** a
  stricter **deep** budget a deep search *additionally* consumes
  (`deep_search_limiter`, `JJHO_DEEP_SEARCH_MAX`, default 30 / window); shared
  window `JJHO_SEARCH_WINDOW` (default 900s). So total per-IP Claude-calling
  searches are bounded and deep stays more tightly bounded than cheap. Only a
  request that actually calls Claude is charged — the `no_api_key` / `no_index`
  / `empty_query` degradation paths make no call and don't spend the budget. A
  throttled request renders the friendly "Easy there, counselor" panel with a
  **429** (never a 500). The route also wraps `db.get_conn()` so an unexpected
  DB error degrades to the "index unavailable" panel instead of 500ing.

## Stack

- **Python 3.11+**, **Flask** (server-rendered, no JS framework), **gunicorn**
  to serve.
- Deps in `requirements.txt` (kept minimal): `flask`, `gunicorn`, `feedparser`
  (RSS ingest), `requests` + `beautifulsoup4` (polite cached scraping),
  `anthropic` (Claude API for Super Search).
- **SQLite** index (episodes + transcripts), gitignored — re-derivable from
  public data, so no off-box backup.
- Package `jjho/`:
  - `web/app.py` — Flask factory `create_app`; routes (`/`, `/healthz`,
    `/login`, `/logout`) + security middleware (shared-password gate, Host/Origin
    CSRF pin, security headers).
  - `web/password_gate.py` — shared-password gate helpers (safe-`next`, per-IP
    login rate limiter). Mirrors the sibling apps.
  - `web/search.py` — Super Search engine (cheap/deep tiers, Claude calls,
    tolerant JSON parsing, graceful degradation). Flask-free/importable.
  - `web/templates/` — `base.html` (courtroom shell, theme-aware, CSP-safe
    system font stacks), `index.html`, `login.html`, `episodes.html` (The
    Docket browser).
  - `web/static/js/episodes.js` — instant client-side docket filter
    (progressive enhancement; the page also filters server-side via `?q=`).
    Served from `/static` because the CSP forbids inline scripts.
- Package `jjho/data/` — the ingest pipeline:
  - `db.py` — SQLite connection + schema (`meta`, `episodes`, `transcripts`),
    idempotent UPSERTs, read helpers. DB path: `data/jjho.db` (override
    `JJHO_DB`; data dir override `JJHO_DATA`). WAL, FK on.
  - `rss.py` — feedparser spine ingest (guid id, `itunes:episode` number,
    title, pub date, blurb, audio + listen URL).
  - `wikipedia.py` — scrapes both episode-list pages; enriches guest bailiff +
    dispute. **Merged by normalized TITLE, not number** — RSS `itunes:episode`
    and Wikipedia's `No.` diverge (~2 ahead through the back catalog).
  - `transcripts.py` — polite MaxFun scraper (crawls the paginated listing to
    map `ep number → transcript URL`, extracts the `<p>` body from `<main>`).
  - `httpclient.py` — shared polite cached HTTP (≥1 req/s, on-disk cache under
    `data/cache/`, identified UA, HTTP/1.1, backoff honoring `Retry-After`).
  - `ingest.py` — the CLI (`python -m jjho.data.ingest`).

**Data caveat — the SQLite DB and scrape caches live under `data/` and are
gitignored.** The `.gitignore`/`.dockerignore` entries are **anchored** (`/data`,
not `data`) so they do NOT swallow the `jjho/data` Python package — a bare
`data/` matches at every depth and would silently drop the package from git and
the Docker image.

## Data sources (see DESIGN.md for the caveats)

- **Episode spine:** podcast RSS (`feeds.simplecast.com/q8x9cVws`) + Wikipedia
  episode tables. Complete, cheap. Powers the episode list + cheap search.
- **Transcript layer:** polite, rate-limited, disk-cached scraping of Maximum
  Fun transcripts (`maximumfun.org/transcripts/judge-john-hodgman/…`). Powers
  deep search; **coverage is partial** (strong recent, patchy old/live) — a
  hard caveat to surface in-app. Only source for who-won.
- When you build the scraper: ≥1s between requests, single-threaded, identified
  User-Agent, backoff on 429/5xx honoring `Retry-After`, on-disk cache, respect
  robots.txt. Never weaken this without explicit approval (mirror taste-twin's
  policy).

## Run / test

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt

# dev server — with no APP_PASSWORD the sign-in gate is OFF (local dev only)
flask --app jjho.web run --port 8080     # home: / , health: /healthz

# production-style
gunicorn --workers 2 --threads 8 -b 0.0.0.0:8080 "jjho.web:create_app()"

# build the episode index (RSS + Wikipedia -> SQLite; idempotent, ~2s)
python -m jjho.data.ingest
# + sample the most-recent transcripts (polite, cached, resumable)
python -m jjho.data.ingest --transcripts --limit 25   # foundation sample
python -m jjho.data.ingest --transcripts --all        # full backfill (slow)
python -m jjho.data.ingest --stats                    # coverage summary only
```

Deps for the pipeline (`feedparser`, `requests`, `beautifulsoup4`) are already
in `requirements.txt`. On first boot with no DB, `/episodes` shows a friendly
"index not built yet — run the ingest" message instead of an error.

Config is via env vars — copy `.env.example` → `.env`. Key ones: `APP_PASSWORD`
(gate on), `SESSION_SECRET` (cookie signing), `ANTHROPIC_API_KEY` (Super
Search), `APP_HOST` (Host/Origin CSRF pin for the deployed hostname).

**Tests:** `pip install -r requirements-dev.txt` then `python -m pytest tests/`
(the venv is Python 3.9 in dev; the code uses deferred annotations so it runs
there and on the 3.11+ target). The suite covers the Super Search helpers, tier
+ escalation logic, and route behaviour; **the Anthropic client is always
mocked — no test makes a real API call.**

## Security posture (keep these invariants)

- **Shared-password gate** (env-gated by `APP_PASSWORD`): when set, every route
  but `/login`, `/logout`, static, `/healthz` redirects to `/login` until a
  signed session marker is present. Password compared with
  `hmac.compare_digest`; only a signed marker is stored (never the raw
  password); cookie is HttpOnly+Secure+SameSite=Lax, ~30-day. Per-IP failed-login
  rate limit. **Unset `APP_PASSWORD` = gate OFF — local dev only, never expose.**
- **`APP_HOST`** pins the Host header on all routes and enforces an
  Origin/Referer CSRF check on POSTs.
- **`Referrer-Policy: same-origin`** (not `no-referrer`) — required so the app's
  own same-origin form POSTs still carry an `Origin` for the CSRF pin.
- Cloudflare Access JWT verification is a **deferred** option (env vars
  documented in `.env.example`), not wired — the shared password is the gate.

## Deploy

Docker container on the home box, on the external `km-tracker_default` network,
through the existing `km-tracker` Cloudflare tunnel, public at
**`jjho.graham-williams.com`**, gated by the shared `APP_PASSWORD`. Deploy from
`main` (`git pull && docker compose up -d --build`). See `DEPLOY.md` (stub) and
`DESIGN.md`.

## Git workflow

- All work on **feature branches** (`feature/<name>`); commit freely there.
- `main` is **protected**: no direct pushes, no force-push. Changes reach `main`
  only via a Pull Request that Graham reviews and merges himself. **Never merge a
  PR to `main` on his behalf.**
- **Security gate before pushing:** run a skeptical review over the diff for
  secrets/PII, injection/auth/exposed-endpoint vulns, dependency/supply-chain
  risk, and data exposure. Any finding → fix → re-run before pushing.
- **Never commit secrets.** `.env` and `*.db`/`data/` are gitignored;
  `.env.example` holds placeholders only.

## Self-maintenance

When you add or change a capability, dependency, command, data source, or
architectural decision, update **this CLAUDE.md and `DESIGN.md`** before the task
is done. These files are how context persists for the next agent/session that
enters the repo — if it's not written down, it's lost.
