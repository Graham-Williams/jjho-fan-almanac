# Deploying JJHo — The Fan Almanac (stub)

> Stub. Filled in when the app actually deploys. The pattern mirrors the
> sibling apps `taste-twin` / `todoist-points` / `km-tracker` on the home box.

Intended posture (see `DESIGN.md` → *Deploy target*):

- One Docker container on the home box, joined to the existing external
  **`km-tracker_default`** network. **No host port mapping** — reachable only
  over the tunnel network.
- Routed through the existing **`km-tracker` Cloudflare tunnel**: ingress rule
  `jjho.graham-williams.com` → `http://jjho-fan-almanac:8080` (before the
  catch-all 404), plus a proxied CNAME `jjho` → `<tunnel-id>.cfargotunnel.com`.
  Single-label subdomain only (Universal SSL cert gotcha).
- **Sign-in:** app-level shared `APP_PASSWORD` (+ `SESSION_SECRET`) in the box's
  untracked `~/jjho-fan-almanac/.env`. Same shared password as the sibling apps.
  Keep `APP_HOST=jjho.graham-williams.com` set (Origin/CSRF pin).
- **Anthropic key:** `ANTHROPIC_API_KEY` in the box `.env` (source:
  `claude-api-key` in the 1Password **Hopper** vault, field `api_key`).
- **State:** named volume `jjho-fan-almanac_jjho-data` at `/app/data` — the
  SQLite index + scrape cache. All re-derivable from public data → **no off-box
  backup**.

Deploy / update (deploy from `main` only):

```bash
cd ~/jjho-fan-almanac
git pull
docker compose up -d --build
docker compose ps            # healthcheck hits /healthz
```

No staging instance (like todoist-points / taste-twin). Preview a feature branch
by deploying it directly onto the box/URL if needed, then realign to `main`.

## On-demand data refresh (issue #6)

The episode index + transcripts (`data/jjho.db`) are re-derivable public data,
**not** committed (gitignored) and **not** backed up. When they need refreshing
(new episodes aired, or to backfill more transcripts):

> **Run the scrape on Graham's Mac, NOT on the box.** Maximum Fun sits behind
> Cloudflare bot-management that challenges the box's datacenter IP (confirmed —
> same issue taste-twin hit with Letterboxd). The Mac's residential IP is not
> challenged. The box only *hosts* the DB; the Mac *generates* it.

Recipe (on the Mac, in this repo):

```bash
cd ~/code/jjho-fan-almanac
.venv/bin/python -m jjho.data.ingest                    # rebuild spine (RSS + Wikipedia)
.venv/bin/python -m jjho.data.ingest --transcripts --all   # full transcript backfill (polite, ~1 min warm cache; cold cache is slower at ≥1 req/s)
.venv/bin/python -m jjho.data.ingest --stats            # confirm coverage (expect ~214 transcripts)
```

Then ship the regenerated `data/jjho.db` into the box's Docker volume
**`jjho-fan-almanac_jjho-data`** (mounted at `/app/data`). **TODO — fill in the
real box recipe** (do NOT run it blindly; the box box path/host live in
personal-assistant memory, not here). Sketch:

```bash
# TODO placeholder — verify container name + volume mount before running.
# scp ~/code/jjho-fan-almanac/data/jjho.db graham@<box>:/tmp/jjho.db
# ssh graham@<box> 'docker cp /tmp/jjho.db jjho-fan-almanac-app-1:/app/data/jjho.db && \
#                   docker compose -f ~/jjho-fan-almanac/docker-compose.yml restart app'
```

**Refresh cadence: on-demand only.** No cron/systemd timer. Coverage recovers
the ~25 PDF-only 2023-era episodes and reaches the ~214 ceiling; episodes 1–384
have no transcript to fetch.

**Future option:** solve the box-side Cloudflare challenge (e.g. a residential
egress path) so refreshes could run directly on the box and drop the
Mac-in-the-loop step. Not built — flagged for issue #6.
