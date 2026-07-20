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
