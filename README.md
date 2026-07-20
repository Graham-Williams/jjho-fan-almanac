# JJHo — The Fan Almanac

An **unofficial fan companion** for the *Judge John Hodgman* podcast (Maximum
Fun) — a reference to the ~760 disputes that have come before the Judge: the
cases, the precedents, the running bits, and (in time) the verdicts.

> **Not affiliated with Judge John Hodgman, John Hodgman, or Maximum Fun.**
> This is a fan project built from public data. All rulings belong to the Judge.

## The four features

1. **Super Search** — describe a half-remembered episode in plain English and
   let the app identify it, powered by the Anthropic Claude API. Search is
   **cheap by default** (episode titles + dispute blurbs); a **"Deep search"**
   checkbox searches full transcripts up front, and a **"search deeper"**
   button lets you escalate the same query to transcripts after a cheap search
   comes up short.
2. **The Book of Settled Law** — a browsable index of Hodgman's precedents
   ("Nostalgia is a toxic impulse," hot dog ≠ sandwich, the Tom Waits
   Principle…), tagged recurring-doctrine vs. one-off ruling and cross-linked
   to the episodes that established them.
3. **Motifs & Running Bits** — the recurring refrains and in-jokes, each marked
   real doctrine vs. fan-lore (some are single-source).
4. **Justice Statistics** — wins by party (complainant vs. defendant), by
   gender, by dispute type. *(Phase 2 — the hardest; see the data caveats.)*

## Data & its limits (the fine print)

- The **episode index** (the spine) — title, date, dispute blurb for all ~760
  episodes — comes from the podcast **RSS feed** plus **Wikipedia** episode
  tables. Complete, cheap, fast. It powers the episode list and cheap search.
- The **transcript layer** (the depth) is scraped politely from Maximum Fun's
  public transcripts and powers deep search. **Transcript coverage is not 100%:
  strong for recent years, patchy for older and live episodes.** So deep search
  is excellent on modern episodes and thinner on the deep back-catalog — a
  missing old episode is a *coverage gap*, not a broken search.
- **Who won / Justice Statistics:** no source records the outcomes — they exist
  only in the audio. Reconstructing them needs an LLM pass over transcripts plus
  human review, and rulings are often split. Hence it's a later phase, and the
  numbers will always carry a caveat.

Everything is stored in a local SQLite index that is **re-derivable from public
data**, so it's gitignored and needs no off-box backup.

See **`DESIGN.md`** for the full architecture, the courtroom visual identity,
and the phased build order.

## Stack

- **Python 3.11+**, **Flask** (server-rendered, no JS framework), served by
  **gunicorn**.
- **feedparser** (RSS), **requests** + **beautifulsoup4** (polite, cached
  scraping), **anthropic** (Claude API for Super Search).
- **SQLite** index (episodes + transcripts).
- Docker for self-hosting behind the existing Cloudflare tunnel; sign-in is an
  app-level shared password (see `DEPLOY.md`, once written).

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt

# Dev server. With no APP_PASSWORD the sign-in gate is OFF (local dev only).
flask --app jjho.web run --port 8080
# → http://127.0.0.1:8080/  (health: /healthz)
```

Production-style:

```bash
gunicorn --workers 2 --threads 8 -b 0.0.0.0:8080 "jjho.web:create_app()"
```

Configuration is via environment variables — copy `.env.example` to `.env` and
fill it in. Notable: `APP_PASSWORD` (turns the sign-in gate on), `SESSION_SECRET`
(signs the session cookie), `ANTHROPIC_API_KEY` (Super Search), `APP_HOST`
(Host/Origin CSRF pin for the deployed hostname).

## Status

Skeleton — the app boots (home + health + sign-in gate). The four features are
built on feature branches per the phased plan in `DESIGN.md`.

## License

MIT — see `LICENSE`.
