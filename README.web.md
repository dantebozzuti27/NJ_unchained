# NJ Unchained — screener

Public-facing read-only screener for the NJ Unchained civic-integrity
platform. Surfaces the L3a fraud-risk view (`derived.v_entity_fraud_risk`)
plus per-entity signal evidence.

Stack:
- Next.js 15 App Router, server components, route handlers
- TypeScript
- Tailwind CSS
- `@neondatabase/serverless` (HTTP-based Postgres client)

## Local development

```bash
cd web
npm install
cp .env.example .env.local
# Edit .env.local and set NEON_DATABASE_URL
npm run dev
# → http://localhost:3000
```

The dev server hot-reloads on file changes. The DB is hit per
request (no client-side fetching, no SWR), so changes to data show
up immediately.

## Production deployment (Vercel)

### 1. Create a Neon Postgres database

1. Sign up at https://neon.tech (free tier: 0.5 GB storage, autosuspend).
2. Create a new project, region `aws-us-east-2` (lowest Vercel latency
   from `iad1`).
3. Copy the **Pooled connection string** from Neon's dashboard
   (Connection details → Pooled connection → psql). Looks like:
   ```
   postgres://user:pass@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```

### 2. Migrate the schema and load data into Neon

From the **repo root** (not `web/`):

```bash
# Activate the backend venv (created with `uv venv` or `python -m venv .venv`)
source .venv/bin/activate
pip install -e .

# Run all migrations in order
PG_DSN='<your neon pooled url>' nj-migrate apply

# Apply seed migrations (release calendar, reference tables, etc.)
PG_DSN='<your neon pooled url>' nj-migrate seed

# Confirm everything applied
PG_DSN='<your neon pooled url>' nj-migrate status | tail -20

# Optional: ingest live federal data (LEIE, SAM, FEC, USAspending, ...)
PG_DSN='<your neon pooled url>' nj-ingest-leie load <leie.csv>
PG_DSN='<your neon pooled url>' nj-ingest-sam-exclusions load <sam.csv>
PG_DSN='<your neon pooled url>' nj-ingest-fec ...
# ... etc; see backend README + AGENTS.md for the full ingest playbook
```

### 3. Create the Vercel project

1. https://vercel.com → Add New → Project → Import `dantebozzuti27/NJ_unchained`.
2. **Root Directory**: `web`  ← critical; without this Vercel tries
   to build from the repo root and finds no `package.json`.
3. **Framework preset**: Next.js (auto-detected after step 2).
4. Build command + Output directory: leave as Vercel defaults.

### 4. Set environment variables

Vercel project → Settings → Environment Variables:

| Name | Value | Environments |
|---|---|---|
| `NEON_DATABASE_URL` | the pooled URL from step 1 | Production + Preview + Development |

### 5. Deploy

The first deployment auto-triggers from the import. Subsequent
deployments auto-trigger on every push to `main` (production) or any
other branch (preview).

## Health check

After deployment:

```bash
curl https://<your-vercel-domain>/api/health
```

Expected:

```json
{
  "ok": true,
  "db_reachable": true,
  "cycle_default": "2024",
  "latency_ms": 47
}
```

If `db_reachable: false`, the env var is unset or wrong. If
`db_reachable: true` but `cycle_default: null`, the schema is present
but no FEC data has been loaded yet (run the ingesters in step 2).

## Routes

| Path | Description |
|---|---|
| `/` | Landing + platform status |
| `/risk` | Top-N entities by risk score; filterable by cycle / kind / limit |
| `/risk/[kind]/[id]` | Per-entity drill: score breakdown + firing signals + evidence URLs |
| `/about` | Methodology |
| `/api/health` | Liveness probe (JSON) |

## Type-check + build (CI guard)

```bash
cd web
npm run typecheck   # tsc --noEmit
npm run build       # next build (full build, fails on type errors)
```

Both commands must pass before a deploy is healthy. The build also
rejects unused symbols and any client-side `process.env` reference
that isn't prefixed `NEXT_PUBLIC_*`.

## What this screener intentionally does NOT do

- **No write surfaces.** All routes are GET / read-only. The L5
  analyst-feedback layer (`governance.fraud_review`) is owned by the
  backend CLI, not this UI.
- **No user accounts.** The data is public; the score is a percentile,
  not a probability. No authn/authz layer is justified.
- **No client-side data fetching.** Every view is a server component
  that issues SQL on demand. This trades request latency for zero
  client bundle weight (the JS payload per page is ~5 KB).
- **No probability of fraud.** See `/about` for the honest framing.
