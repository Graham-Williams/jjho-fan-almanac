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
**521 enriched from Wikipedia** (matched by title). Transcript coverage: the
full `--all` backfill stores **214 transcripts** (of 785 numbered) — the true
ceiling. Transcripts exist only from ~**episode #385** onward (1–384 were never
transcribed) and the newest handful lag; this includes ~25 PDF-only episodes
recovered via the PDF-extraction path. See *Data sources* + `DESIGN.md`.

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

- **Python 3.12** (the image is `python:3.12-slim`, CI runs 3.12). The pinned
  dependency set needs **>= 3.10** — `anthropic` 1.x and `gunicorn` 25+ both
  dropped 3.9, so the Mac's system `python3` (3.9) can no longer build a dev
  venv for this repo (see "Dependency pinning" below).
- **Flask** (server-rendered, no JS framework), **gunicorn** to serve.
- Deps in `requirements.txt` (kept minimal): `flask`, `gunicorn`, `feedparser`
  (RSS ingest), `requests` + `beautifulsoup4` (polite cached scraping), `pypdf`
  (PDF-era transcripts), `anthropic` (Claude API for Super Search). **All pinned
  exactly (`==`) — see "Dependency pinning" below before changing that.**
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
    Docket browser), `search.html` (Super Search), and `_macros.html` — the
    shared **`transcript_badge`** macro (transcript-provenance labeling, below).
  - `web/static/js/episodes.js` — instant client-side docket filter
    (progressive enhancement; the page also filters server-side via `?q=`).
    Served from `/static` because the CSP forbids inline scripts.
- Package `jjho/data/` — the ingest pipeline:
  - `db.py` — SQLite connection + schema (`meta`, `episodes`, `transcripts`),
    idempotent UPSERTs, read helpers. DB path: `data/jjho.db` (override
    `JJHO_DB`; data dir override `JJHO_DATA`). WAL, FK on. **Schema v2** added
    `transcripts.source` (`'maxfun'`|`'asr'`) + `asr_model` for transcript
    provenance — an idempotent PRAGMA-guarded `ALTER TABLE` migration in
    `init_schema` that backfills legacy rows to `source='maxfun'` (see the
    two-tier transcript model below).
  - `rss.py` — feedparser spine ingest (guid id, `itunes:episode` number,
    title, pub date, blurb, audio + listen URL).
  - `wikipedia.py` — scrapes both episode-list pages; enriches guest bailiff +
    dispute. **Merged by normalized TITLE, not number** — RSS `itunes:episode`
    and Wikipedia's `No.` diverge (~2 ahead through the back catalog).
  - `transcripts.py` — **Tier 1** polite MaxFun scraper (crawls the paginated
    listing to map `ep number → transcript URL`, extracts the `<p>` body from
    `<main>`). Writes `source='maxfun'`. **PDF fallback:** ~25 episodes (mostly
    2023-era) publish the transcript as a downloadable PDF, not inline HTML —
    the page's `<main>` is only a "Download transcript (pdf)" stub. When the
    inline text is sub-threshold and the page carries a
    `maximumfun.org/wp-content/…/*.pdf` link, the PDF is fetched (`fetch_bytes`)
    and parsed with **pypdf** (`extract_pdf_text`, guarded — a corrupt PDF
    stores `has_transcript=0`, never crashes); the PDF URL becomes the
    transcript's `source_url`. **Listing-crawl hardening:** `build_listing_map`
    distinguishes a genuine end-of-listing (a valid page with zero links) from a
    transient fetch failure (retry, then skip the page and keep crawling) — the
    earlier `if not html: break` aborted the whole newest-first crawl on one
    flaky page (the 189-vs-214 non-determinism).
  - `asr.py` — **Tier 2** local Whisper transcription. Stream-downloads the
    episode mp3, runs MLX Whisper (`mlx-community/whisper-large-v3-turbo`),
    stores the text with `source='asr'` + `asr_model`, and **deletes the temp
    audio immediately** (disk is tight — never accumulate mp3s). Resumable +
    idempotent; run `python -m jjho.data.asr [--limit N] [--model ID]`.
  - `httpclient.py` — shared polite cached HTTP (≥1 req/s, on-disk cache under
    `data/cache/`, identified UA, HTTP/1.1, backoff honoring `Retry-After`).
    `fetch()` returns decoded text (HTML, `<hash>.html` cache); **`fetch_bytes()`**
    is its binary sibling for PDFs — identical politeness, `<hash>.bin` cache,
    returns raw `bytes` (never decode a PDF through `fetch()`).
  - `ingest.py` — the CLI (`python -m jjho.data.ingest`).

**Data caveat — the SQLite DB and scrape caches live under `data/` and are
gitignored.** The `.gitignore`/`.dockerignore` entries are **anchored** (`/data`,
not `data`) so they do NOT swallow the `jjho/data` Python package — a bare
`data/` matches at every depth and would silently drop the package from git and
the Docker image.

## Data sources (see DESIGN.md for the caveats)

- **Episode spine:** podcast RSS (`feeds.simplecast.com/q8x9cVws`) + Wikipedia
  episode tables. Complete, cheap. Powers the episode list + cheap search.
- **Transcript layer — two tiers, distinguished by `transcripts.source`:**
  - **Tier 1 (`maxfun`, ~214 eps, ep 385+):** the official *human* transcripts,
    politely scraped from Maximum Fun
    (`maximumfun.org/transcripts/judge-john-hodgman/…`; `transcripts.py`).
    **Honest coverage reality for MaxFun alone:** official transcripts exist
    only from ~**episode #385** onward (1–384 were never transcribed by MaxFun);
    the **true MaxFun ceiling is ~214 transcripts** (of 785 numbered) and the
    `--all` backfill reaches it — including the ~25 PDF-only episodes via the
    PDF-extraction path. The newest handful lag (production delay). So MaxFun on
    its own is *partial* — strong-recent, none-old.
  - **Tier 2 (`asr`, the rest):** machine-generated transcripts we produce
    locally with **MLX Whisper** (`mlx-community/whisper-large-v3-turbo`,
    ~17-20x real-time on Graham's Mac, excellent quality) from the show's own
    audio (`asr.py`) — closing the ~570 episodes (incl. all of 1–384) MaxFun
    never transcribed. **With ASR, total transcript coverage reaches ~100%** —
    the two tiers differ in *kind* (human vs machine), not in whether coverage
    is complete. **These are machine-generated**, and the UI now labels them
    honestly: wherever a transcript's content or availability is surfaced (The
    Docket rows + Super Search result cards), the shared `transcript_badge`
    macro renders a muted **"🤖 Auto-generated"** pill (tooltip: *"Machine-
    transcribed with Whisper; may contain errors."*) for `source='asr'` and a
    subtle **"✓ Official transcript"** marker for `source='maxfun'` — so an ASR
    transcript is never mistaken for an official one. `source`/`asr_model` are
    threaded DB→template through `list_episodes`, `spine_for_search`, and
    `transcripts_for_terms` (all now return `transcript_source` + `asr_model`).
    Search behavior is unchanged — this is provenance/UX only.
  - Together they power deep search + who-won. The **ASR batch runs on Graham's
    Mac, not the box** (Whisper + audio download); the resulting DB is shipped
    to the box exactly like the MaxFun-scraped data. Design: **stream-download →
    transcribe → delete the mp3** (a 200 MB byte cap per file, temp audio never
    kept — disk is tight). Resumable/idempotent (a stored body skips the ep) and
    per-episode fault-isolated (one failure is logged + skipped, never aborts).
- When you build the scraper: ≥1s between requests, single-threaded, identified
  User-Agent, backoff on 429/5xx honoring `Retry-After`, on-disk cache, respect
  robots.txt. Never weaken this without explicit approval (mirror taste-twin's
  policy).

## Run / test

```bash
# Python 3.12 — NOT the Mac's system python3 (3.9): the pinned deps need >= 3.10
uv venv --python 3.12 .venv && source .venv/bin/activate
pip install -r requirements.txt

# dev server — with no APP_PASSWORD the sign-in gate is OFF (local dev only)
flask --app jjho.web run --port 8080     # home: / , health: /healthz

# production-style
gunicorn --workers 2 --threads 8 -b 0.0.0.0:8080 "jjho.web:create_app()"

# build the episode index (RSS + Wikipedia -> SQLite; idempotent, ~2s)
python -m jjho.data.ingest
# + sample the most-recent MaxFun (Tier 1) transcripts (polite, cached, resumable)
python -m jjho.data.ingest --transcripts --limit 25   # foundation sample
python -m jjho.data.ingest --transcripts --all        # full backfill (slow)
python -m jjho.data.ingest --stats                    # coverage summary only

# Tier 2 — self-transcribe the episodes MaxFun never covered, via local Whisper.
# Runs on Graham's MAC ONLY. Needs ffmpeg, the cached HF model, and mlx_whisper
# — which is deliberately NOT in requirements*.txt (Mac-only, never shipped in
# the image). Install it into the dev venv by hand: `pip install mlx-whisper`.
# The model itself lives in ~/.cache/huggingface and survives a venv rebuild.
# resumable — safe to Ctrl-C and re-run; stream-downloads + deletes each mp3.
.venv/bin/python -m jjho.data.asr            # full missing backfill (~570 eps, hours)
.venv/bin/python -m jjho.data.asr --limit 1  # smoke test / one newest gap
.venv/bin/python -m jjho.data.asr --model <hf-repo-id>   # override the model
# ~17-20x real-time; prints a per-source coverage summary at the end. After a
# run, ship data/jjho.db to the box like the MaxFun data.
```

Deps for the pipeline (`feedparser`, `requests`, `beautifulsoup4`) are already
in `requirements.txt`. On first boot with no DB, `/episodes` shows a friendly
"index not built yet — run the ingest" message instead of an error.

Config is via env vars — copy `.env.example` → `.env`. Key ones: `APP_PASSWORD`
(gate on), `SESSION_SECRET` (cookie signing), `ANTHROPIC_API_KEY` (Super
Search), `APP_HOST` (Host/Origin CSRF pin for the deployed hostname).

**Tests:** build the dev venv on **Python 3.12** (`uv venv --python 3.12 .venv`
— the pinned deps need >= 3.10, so a venv made from the Mac's system 3.9 fails
to install with a confusing "no matching distribution" for versions that
definitely exist), then `pip install -r requirements-dev.txt` and
`python -m pytest tests/`. The suite covers the Super Search helpers, tier +
escalation logic, and route behaviour; **the Anthropic client is always
mocked — no test makes a real API call**, which is also why an `anthropic`
version bump has to be reasoned about rather than trusted to the suite.

### Dependency pinning — exact `==`, never a bounded range

`requirements.txt` / `requirements-dev.txt` pin **every** direct dependency to an
exact version. This is a deliberate convention (adopted 2026-09-27, matching
baby-pool and km-tracker); do not "tidy" it back into `>=` floors or ranges.
Two reasons:

1. **Reproducibility — and this repo is the proof.** The Dockerfile runs
   `pip install -r requirements.txt`, so a bare `>=` floor installs whatever is
   newest at *image-build* time. This file said `gunicorn>=23.0` and
   `anthropic>=0.40` while the box was running **gunicorn 26.2.0** and
   **anthropic 1.7.0** — two major-version bumps that reached production on an
   image rebuild, with no PR, no review and no test run. An unbounded floor is
   not a dependency declaration, it is a promise to install the future.
2. **Dependabot classifies exact pins correctly and bounded ranges incorrectly.**
   `dependabot/fetch-metadata` misparses a two-sided range: `pypdf>=6.14.2,<7`
   produced the PR title `Update pypdf requirement from <7,>=6.14.2 to
   >=6.19.0,<7` and `update-type: version-update:semver-major` for what was a
   **minor** bump, so `ci.yml`'s auto-merge gate (correctly) refused it and PR
   #21 stuck forever. Unbounded entries parsed fine — PR #29 was reported as a
   major because `gunicorn` 23 -> 26 genuinely is one. With `==` the title is
   `Bump X from A to B` and minor/patch bumps auto-merge on green CI as intended.

Rules when touching these files:

- Pin to the version **actually deployed**, verified rather than recalled:
  `ssh graham@100.101.1.28 'docker exec jjho-fan-almanac python3 -m pip freeze'`.
  Never let a pin land *below* what prod runs — that is a silent downgrade on the
  next deploy.
- Let a Dependabot PR do the upgrading. Don't fold a version bump into an
  unrelated change; a correctly-titled bump now auto-merges on its own.
- **Transitive** deps (Werkzeug, httpx2, soupsieve, ...) are intentionally left
  to the resolver — pin one only for a specific reason, stated in a comment next
  to it.
- The only `anthropic` API surface this app uses is
  `messages.create(model, max_tokens, system, messages, thinking)` plus
  `resp.content[].type/.text`, which is unchanged across 0.x -> 1.x. The 1.x
  breaking changes that *could* bite here are the Python >= 3.10 floor, the
  removal of the sampling params (`temperature`/`top_p`/`top_k` — unused) and of
  Text Completions (unused). Re-check that list before moving the pin again.

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
- **HTTPS is enforced at the ORIGIN, not just at the edge** (issue #17).
  Cloudflare's zone-wide *Always Use HTTPS* already 301s http→https, but that is
  one dashboard toggle away from regressing, so the app does it too:
  - **`_https_redirect` (`before_request`, registered FIRST — before the
    password gate)** 307s to `https://<APP_HOST><target>`. ⚠️ **It redirects ONLY
    when `X-Forwarded-Proto`, trimmed and case-folded, is *exactly* `http`.**
    That header rule IS the exemption list — there are deliberately no per-path
    exemptions. The compose healthcheck
    (`urlopen('http://127.0.0.1:8080/healthz')`) and any other in-network probe
    send no `X-Forwarded-Proto`, so they are untouched; a blanket "scheme is
    http" rule would redirect the healthcheck and mark the container unhealthy
    forever. A chained-proxy list value (`http, https`) also fails open.
    - ⚠️ **`.strip().lower()` is load-bearing** — URI schemes are
      case-INSENSITIVE (RFC 3986 §3.1, RFC 9110). The first cut compared
      case-sensitively and failed in the *dangerous* direction: measured live
      against gunicorn, `X-Forwarded-Proto: HTTP` was served **200 over plain
      http**. All five sibling repos normalise the same way. Normalising must
      NOT start matching the multi-hop `http, https` — there is a test for it.
  - **The redirect is a `307`, carrying `Cache-Control: no-store` and
    `Vary: X-Forwarded-Proto`.** Its `Location` is byte-identical to the
    requested URL, and a `301` with no freshness information is heuristically
    cacheable *indefinitely* (RFC 9111 §4.2.2). In the exact scenario this
    feature exists for — the edge's *Always Use HTTPS* regressing — a shared
    cache could store that self-referential redirect (`/static/*.css|.js` are
    precisely what Cloudflare caches by default) and replay it to **https**
    visitors: broken assets, or a loop. A misconfigured `APP_HOST` under a
    `301` would likewise be sticky in every visitor's browser with no way to
    recall it. `307` also preserves the method, so a plain-http POST is re-sent
    over https rather than silently downgraded to a bodiless GET. HSTS already
    supplies the durable client-side upgrade, so permanence buys nothing.
    **Do not "restore" the 301.** **`Vary: X-Forwarded-Proto` is on EVERY
    response, not just the 307** (B2, from the 2026-09-19 break-staging sweep):
    the 200s/302s the redirect gates are equally scheme-dependent, so a shared
    cache could otherwise store an https-served 200 and later hand it to a
    plain-http request. Stamped in the security-headers `after_request` with
    **`resp.vary.add()`, never `headers["Vary"] = …`** — Flask appends `Cookie`
    to `Vary` itself when the session is touched, and assignment would silently
    clobber it; `.vary.add()` is idempotent, so the 307's own value is not
    doubled.
  - **The target host is always the configured `APP_HOST` pin, never the
    request's own Host/URL** — reflecting the Host would be an open redirect.
    `APP_HOST` unset or not a bare hostname ⇒ **redirecting is OFF (fail open)**,
    so local dev and the test suite keep working. **A bare hostname must contain
    at least one DOT and its final label may not be all-digits** (B1, same
    sweep) — a public origin pin always has a dot, and without that rule
    `APP_HOST=localhost` (or a bare IPv4 literal, or the compose service name
    `jjho-fan-almanac`) *validated*, so every plain-http visitor got a live
    `Location: https://localhost/…`: broken for everyone, and silent precisely
    BECAUSE the value passed, so the fail-open branch never fired. Those values
    now fail open. Strictly a tightening — `jjho.graham-williams.com`, the apex
    and the 253-char boundary host all still pass. `_HOSTNAME_RE` is kept
    **byte-identical** across km-tracker, taste-twin, hopper-dashboard and
    baby-pool.
  - **`request.full_path` must NEVER be used to build the target** — Flask
    percent-*decodes* `request.path`, so `/a%20b` would be rebuilt as `/a b` and
    `/a%2Fb` as `/a/b`. `_request_target()` reads the raw request line from
    `RAW_URI`/`REQUEST_URI` (both gunicorn and werkzeug set it, incl. the test
    client) and only re-encodes the decoded path as a fallback. It also refuses
    a `//`- or `/\`-prefixed target and anything with a control character, so
    the `Location` header can't be split or made to read as another authority.
  - **`Strict-Transport-Security: max-age=31536000`** on every response
    (`_security_headers`). **No `includeSubDomains`** (it would commit every
    sibling app on `graham-williams.com`) and **no `preload`** (irreversible) —
    each hostname owns its own policy, matching the apex landing page. Browsers
    ignore HSTS over plain http (RFC 6797), so sending it unconditionally is
    safe and can't break local dev.
  - ⚠️ **`before_request` HOOK REGISTRATION ORDER IS LOAD-BEARING.** Flask runs
    `before_request` hooks in registration order, and the entire security
    argument for this feature is that `_https_redirect` is defined *above*
    `_password_gate` in `create_app()` — a plain-http visitor is upgraded
    before the login form (or any credential) is ever handled in the clear.
    Do not reorder or reshuffle those hook definitions. Pinned structurally by
    `test_hook_registration_order_is_load_bearing`.
  - Session cookie stays `Secure` + `HttpOnly` + `SameSite=Lax` (asserted in
    `tests/test_https_enforcement.py`, which also covers all of the above).
- Cloudflare Access JWT verification is a **deferred** option (env vars
  documented in `.env.example`), not wired — the shared password is the gate.


### CI (`.github/workflows/ci.yml`)

Runs on every pull request (the `pull_request` trigger is deliberately
unfiltered, so a stacked PR based on another branch still gets CI) and on
pushes to `main`. One `test` job:
Python 3.12, `pip install -r requirements-dev.txt`, `python -m pytest -q`
(120 tests). No network — the scrapers are mocked.
Actions are pinned by commit SHA, not by tag — a tag can be re-pointed at
different code. Dependabot's `github-actions` ecosystem keeps the pins fresh;
refresh one by hand with
`gh api repos/<owner>/<action>/git/ref/tags/<tag> --jq .object.sha`.
Every job carries `timeout-minutes` (a job with no timeout burns a runner for
six hours when it stalls — km-tracker issue #84 was filed for exactly that).


### Dependabot auto-merge — what merges itself, what stops for Graham

`ci.yml` has a `dependabot-auto-merge` job. **Merges itself**, with no review,
only when ALL of these hold:

1. The PR author is `dependabot[bot]`, and `dependabot/fetch-metadata` confirms
   the head commit was authored by Dependabot **and carries a verified
   signature**. Nobody can hand-craft a PR into this path.
2. **Every other job in the same workflow run succeeded** — that is literally
   the `needs:` list, which names every sibling job.
3. The update is **semver-minor**, **semver-patch**, or a **Docker digest
   refresh** (same image tag, rebuilt digest).

**Always stops for Graham:**

- Any **major** version bump — *including a security update*. A security fix
  that crosses a major still waits for him.
- Any PR where no semver level could be derived and it is not a digest refresh.
  An unknown update type is an absence of signal, never a pass.
- Anything whose tests failed, errored, or did not run. **A run with no checks
  can never merge**, because the merge step is unreachable unless the jobs it
  needs actually reported success.

**The gate is `needs:`, not branch protection — do not "simplify" it to
`gh pr merge --auto`.** GitHub's native auto-merge only waits for checks that
branch protection marks as *required*, and none of these repos define required
status checks (baby-pool is private, where the plan offers no branch protection
or rulesets at all). On such a repo `--auto` silently degrades to "merge now",
which would merge a PR whose tests never ran. Depending on the sibling jobs
behaves identically on every repo, protected or not. For the same reason
`allow_auto_merge` is deliberately left **off** — the design does not use it.

The trigger is `pull_request`, **not** `pull_request_target`. The auto-merge
job checks out nothing and runs no PR code; the only job that runs repository
code is `test`, which holds a read-only token. A `pull_request` run from a fork
gets a read-only token regardless of the `permissions:` block, so the write
scopes are unreachable from a fork.

Each run writes its decision and reasoning to the job summary, so the reason a
particular PR did or did not merge is always on the run page.

**Known false negative (safe):** for a bounded requirement range —
`Update X requirement from <4.0,>=3.0 to >=3.1.3,<4.0` — `fetch-metadata`'s
regex reads the bounds as the versions and reports **semver-major**, so these
stop for Graham even though they are minor floor bumps. That is the fail-closed
direction, and it is left alone on purpose: overriding a "major" verdict with
home-grown parsing would turn a safe stop into a possible unsafe merge.

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
