# DESIGN — JJHo, The Fan Almanac

Design notes for the unofficial *Judge John Hodgman* fan companion. Keep this
current as the architecture evolves (see the self-maintenance note in
`CLAUDE.md`).

> Unofficial fan project. Not affiliated with Judge John Hodgman, John Hodgman,
> or Maximum Fun.

## The product

A reference to the ~760 disputes heard on *Judge John Hodgman* (Maximum Fun),
with a courtroom / "settled law" identity. Four features:

### 1. Super Search
Describe a half-remembered episode in natural language ("the one about whether a
Pop-Tart is a sandwich") and the app identifies it, using the **Anthropic Claude
API**.

UX — cost-tiered, so we don't pay to read every transcript on every query:

- **Cheap search by default.** The query runs over the episode **titles +
  dispute blurbs** only (the RSS/Wikipedia spine). Fast and nearly free.
- **"Deep search" checkbox.** When ticked, the query searches the **full
  transcripts** up front — for when the user knows only a passing detail from
  mid-episode.
- **"Search deeper" escalation button.** Shown *after* a cheap search when the
  user isn't satisfied; re-runs the same query over transcripts without making
  them retype it.

**Built (Phase 2).** `GET /search`, engine in `jjho/web/search.py`, read helpers
in `jjho/data/db.py`. **Cheap** = one Claude call over the whole spine
(`spine_for_search`); **deep** = a bounded candidate set (`transcripts_for_terms`)
then Claude over excerpts, with the cheap matches unioned in so **deep ⊇ cheap**.
- **Deep-candidate mechanism = keyword-LIKE, not FTS5.** Salient query terms
  (stopword-stripped) drive an `OR`-of-`LIKE` scan of `transcripts.full_text`,
  ranked by distinct-terms-matched then total occurrences, capped at ~22, each
  carrying whitespace-collapsed excerpts. Chosen over an FTS5 virtual table
  because it needs **zero schema migration**, works on the existing gitignored
  DB, and the corpus is small (~760 episodes, partial transcripts) — LIKE is
  fast enough and deterministic. Revisit FTS5 only if the corpus or query volume
  grows. Never feeds full transcripts to the model — only titles + excerpts.
- **Models (from the `claude-api` skill):** cheap = **Haiku 4.5**
  (`JJHO_SEARCH_MODEL_CHEAP`), deep = **Sonnet 5** (`JJHO_SEARCH_MODEL_DEEP`,
  thinking disabled). Overridable via env.
- **Degrades, never 500:** no API key / `anthropic` import / empty index all
  render friendly panels. Light per-IP throttle on deep searches.

### 2. The Book of Settled Law
A browsable index of Hodgman's **precedents** — "Nostalgia is a toxic impulse,"
hot dog ≠ sandwich, the Tom Waits Principle, etc. Each entry is **tagged
recurring-doctrine vs. one-off ruling** and **cross-linked to the episode(s)**
that established or invoked it.

### 3. Motifs & Running Bits
The recurring **refrains and in-jokes**, each marked **real doctrine vs.
fan-lore** — some are genuine cross-episode doctrine, others are single-source
bits fans have canonized. The distinction is surfaced honestly (some entries are
one citation).

### 4. Justice Statistics — Phase 2 / hardest
Wins by **party** (complainant vs. defendant), by **gender**, by **dispute
type**. This is the hardest feature because of the data caveat below.

## Data architecture

Two layers: a cheap complete **index** (the spine) and an expensive partial
**transcript layer** (the depth).

### Index / spine — ~760 episodes, complete
- **Sources:** the podcast **RSS feed** (`feeds.simplecast.com/q8x9cVws`) +
  **Wikipedia** episode-list tables.
- **Fields:** title, publish date, dispute blurb/description.
- **Properties:** complete, cheap, fast to (re)build. Powers the episode list
  and **cheap Super Search**.

### Transcript layer / depth — two tiers (provenance in `transcripts.source`)
- **Tier 1 — `maxfun` (official human transcripts, ~214 eps, ep 385+):** full
  transcripts scraped **politely** (rate-limited, single-threaded, on-disk
  cached, robots-aware — mirror taste-twin's scraping discipline) from **Maximum
  Fun** (`maximumfun.org/transcripts/judge-john-hodgman/…`). Strong-recent /
  patchy-old on its own.
- **Tier 2 — `asr` (machine transcripts, the remaining ~570 eps):** we
  self-transcribe the show's own audio with **local MLX Whisper**. Model
  `mlx-community/whisper-large-v3-turbo` — the best speed/quality trade-off
  measured (~17-20x real-time on Graham's Mac, quality on par with the official
  transcripts). This closes the gap to **true ~100% transcript coverage**.
  - **Design (`jjho/data/asr.py`): stream-download the mp3 → transcribe →
    delete the mp3**, always in a `finally` (disk is tight; a 200 MB byte cap
    bounds each download). Resumable + idempotent (a stored body of either
    source short-circuits the episode) and per-episode fault-isolated (one
    download/transcribe failure is logged + skipped, never aborts the batch).
    Newest-first so the most-listened recent gaps fill first.
  - **Runs on Graham's Mac, not the box** (Whisper + the ~570 audio downloads);
    the resulting `data/jjho.db` is shipped to the box like the MaxFun data.
    `.venv/bin/python -m jjho.data.asr [--limit N] [--model ID]`.
  - **Honesty:** ASR transcripts are **machine-generated** — the UI must label
    them (e.g. "auto-transcribed") wherever `source='asr'`. The `source` /
    `asr_model` columns exist now; the visible label is a follow-up.
- **Powers:** deep Super Search + who-won extraction.

### ⚠️ Coverage caveat (surface this in-app too)
With Tier 2 ASR, transcript coverage approaches **~100%**, but the two tiers
differ in kind: Tier 1 is a verified human transcript, Tier 2 is a machine
approximation (occasional mishearings, no speaker labels). In the UI, keep the
provenance visible — an "auto-transcribed" marker on `source='asr'` episodes —
so a user knows an ASR transcript is best-effort, not authoritative, and frame
any residual gap as a *coverage gap*, not a broken search.

### ⚠️ Who-won / stats caveat
**No source records episode outcomes** — they exist only in the audio.
Justice Statistics therefore requires an **LLM-over-transcripts extraction pass
plus human review**, and many rulings are **split** (partial findings for both
parties). That's why it's Phase 2 and why the numbers always carry a caveat.

### Storage
Everything lives in a local **SQLite** index (episodes + transcripts + derived
tables). It is **gitignored** and **re-derivable from public data**, so there is
**no off-box backup** — a disk loss costs a re-scrape, not data.

### Built (Phase 1 foundation) — schema + ingest

The pipeline lives in `jjho/data/` (CLI: `python -m jjho.data.ingest`). Schema
(`meta` schema-version row + two tables):

- **`episodes`** — PK `id` (RSS guid), `number`, `title`, `pub_date`,
  `pub_date_raw`, `blurb` (RSS summary, back-filled from Wikipedia dispute when
  empty), `wiki_dispute`, `audio_url`, `listen_url`, `guest_bailiff`,
  `has_transcript`, source flags `from_rss` / `from_wikipedia`, timestamps.
- **`transcripts`** — PK `episode_id` (FK → episodes), `full_text`,
  `source_url`, `fetched_at`, `has_transcript`, plus (**schema v2**) `source`
  (`'maxfun'`|`'asr'`) + `asr_model` (the Whisper model id for ASR rows, NULL
  for maxfun). The v1→v2 migration in `init_schema` is an idempotent
  PRAGMA-guarded `ALTER TABLE ADD COLUMN` that backfills legacy rows to
  `source='maxfun'`, so an existing gitignored DB upgrades in place on next open.

All writes are idempotent UPSERTs; both the MaxFun scraper and the ASR batch are
resumable (skip episodes already stored — the scraper also disk-caches under
`data/cache/`).

**Key finding — merge by TITLE, not number.** The podcast RSS `itunes:episode`
numbers and Wikipedia's `No.` column **diverge** (Wikipedia counts an early
pilot/specials differently and runs ~2 ahead through the back catalog), so the
Wikipedia enrichment is joined on **normalized title** (~96% match). RSS
numbering stays authoritative for the spine.

**Measured coverage (first real run):** 819 feed items (784 numbered), 521
episodes enriched from Wikipedia. Transcript sample — 25 most-recent = 4/25
(the newest ~14 episodes have no transcript yet: Maximum Fun publishes them on a
lag); 100 most-recent = 51/100; and ~59% for episodes old enough to be
transcribed (≤ ep 768). This confirms the coverage caveat and is surfaced in
The Docket's fine print. **Follow-up:** a background full backfill
(`--all`, ~760 episodes at ≥1 req/s) once the pipeline is merged.

## Visual identity / styling

Courtroom / "settled law" aesthetic:

- **Ground:** parchment. **Accents:** oxblood, brass, bottle-green.
- **Type:** a literary **serif** for body/headings — stack
  `"Hoefler Text", "Iowan Old Style", Palatino, "Palatino Linotype",
  "Book Antiqua", Georgia, serif` (**no webfont CDN** — Artifact/CSP-safe,
  system stacks only). **Monospace** for case numbers/dockets.
- **Theme-aware:** light + dark, via `prefers-color-scheme` plus
  `:root[data-theme=...]` overrides (all colors are CSS variables in
  `jjho/web/templates/base.html`).
- **Copy** leans into show phrases — "Bailiff — swear them in," "Enter the
  court," "All rise." Product name: **The Fan Almanac**.

## Deploy target

Same pattern as the sibling apps (km-tracker / todoist-points / taste-twin) —
**document now, wire at deploy time**:

- Docker container on the home box, joined to the existing **`km-tracker_default`**
  network, routed through the existing **`km-tracker` Cloudflare tunnel**
  (service `jjho-fan-almanac` → its port `8080`).
- Public at **`jjho.graham-williams.com`** — a free single-label subdomain of
  Graham's existing `graham-williams.com` (no purchase; remember the cert
  gotcha — single-level subdomains only).
- Sign-in via the **same app-level shared `APP_PASSWORD`** as the siblings: a
  signed HttpOnly/Secure/SameSite session cookie, per-IP failed-login rate
  limit, `APP_HOST` Origin/CSRF pin. (Cloudflare Access JWT verification is
  left as a documented, deferred option.)
- **Anthropic API key** via `ANTHROPIC_API_KEY` (source: `claude-api-key` in the
  1Password **Hopper** vault, field `api_key`).
- No off-box backup needed (state is re-derivable public data).

A `DEPLOY.md` runbook is filled in when the app actually deploys.

## Phased build order

1. **Foundation** *(this scaffold)* — booting Flask skeleton, shared-password
   gate, courtroom shell, Docker/compose, docs. Then: RSS + Wikipedia ingest →
   SQLite episode index → episode list + cheap keyword search.
2. **Super Search** — Claude-powered identification over the spine (cheap) with
   the "Deep search" checkbox + "search deeper" escalation once the transcript
   layer exists. Build the polite MaxFun transcript scraper here.
3. **The Book of Settled Law + Motifs & Running Bits** — curated precedent and
   motif indexes, tagged (doctrine vs. one-off / real vs. fan-lore) and
   cross-linked to episodes.
4. **Justice Statistics** — the LLM-over-transcripts outcome-extraction pass +
   human review; ship with prominent caveats.

## Repo layout

```
jjho/                    Python package
  __init__.py
  data/                  Ingest pipeline (the index + transcript layers)
    db.py                SQLite schema + idempotent UPSERTs + read helpers
    rss.py               feedparser spine ingest
    wikipedia.py         episode-table scrape + title-based enrichment
    transcripts.py       Tier 1: polite MaxFun transcript scraper (listing + body)
    asr.py               Tier 2: local Whisper backfill (stream/transcribe/delete)
    httpclient.py        shared polite cached HTTP (≥1 req/s, HTTP/1.1)
    ingest.py            CLI: python -m jjho.data.ingest [--transcripts ...]
  web/                   Flask app
    __init__.py          create_app factory export
    app.py               routes (+ /episodes) + security middleware
    password_gate.py     shared-password gate helpers + rate limiter
    templates/           base.html (courtroom shell), index, login, episodes
    static/js/           episodes.js (instant docket filter; CSP-safe external)
Dockerfile               gunicorn image
docker-compose.yml       tunnel-network wiring (jjho.graham-williams.com)
requirements.txt         flask, gunicorn, feedparser, requests, bs4, anthropic
```

> **`.gitignore`/`.dockerignore` gotcha:** ignore the data dir as `/data`
> (anchored), never bare `data/` — the latter also matches the `jjho/data`
> Python package and would silently drop it from git and the image.
