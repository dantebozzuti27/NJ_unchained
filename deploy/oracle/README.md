# Oracle Cloud Always Free — NJ platform data box

Provisioning runbook for moving the national-scale substrate off Neon's
512 MB free tier onto a self-hosted Postgres 16 on Oracle Cloud's
**Always Free** tier (`VM.Standard.A1.Flex`, 4 OCPU ARM, 24 GB RAM,
200 GB block). Operating cost stays **$0/month** per `AGENTS.md`.

## Why this box exists (read this first)

The 2026-06-10 national validation run settled the original question
("more data → more precise?"). It does **not** improve supervised
validation: scaling the provider universe 36× (51k → 1.85M) left the
usable label set at ~57/yr, because (a) only ~10% of LEIE exclusions
carry an NPI, (b) "excluded-and-still-billing" is genuinely rare, and
(c) that label measures a *different* offense than the over-utilization
detectors. See `work_left.txt` (session 2026-06-10b).

So the honest rationale for this box is **serving**, not validation:
hold the full national CMS substrate so the `/leads` queue can rank the
**whole country's** top undetected over-utilization providers by Medicare
scale — a product upgrade Neon's 512 MB cap cannot hold. If you only want
the validation answer, you already have it locally at $0; you do **not**
need this box for that.

## Human gate (only you can do this)

Creating the Oracle account requires **your identity + a payment card for
verification**. Always Free resources are not charged, but I cannot sign
up on your behalf. Everything after the account exists is automated here.

Known Always-Free caveats to expect:
- A1 (ARM) capacity is often scarce in popular regions — you may hit
  "Out of host capacity." Retry, try other availability domains, or pick
  a less-busy home region at signup.
- Idle Always Free **compute** instances can be reclaimed on pure-free
  accounts. Keep it doing nightly work (the Dagster daemon) or upgrade to
  "Pay As You Go" (still $0 if you stay within Always Free limits — but
  that removes the no-card-charge guardrail, so decide deliberately).

## Steps

### 1. Create the instance
- Region: pick one with A1 capacity. Shape: **VM.Standard.A1.Flex**,
  4 OCPU / 24 GB. Image: **Canonical Ubuntu 24.04 (aarch64)**.
- Boot volume: 50 GB is plenty for the OS; the national substrate (~5 GB
  in Postgres today, headroom for cross-cycle growth) lives on the boot
  or a second block volume. Always Free gives 200 GB total block storage.
- Add your SSH public key.
- Expand **"Advanced options → Management → Cloud-init script"** and paste
  `deploy/oracle/cloud-init.yaml`. **Change the placeholder DB password**
  in that file first (`CHANGE_ME_njlocal`).

### 2. Networking — do NOT expose Postgres
Leave the default security list closed except SSH (22). Postgres listens
on `localhost` only (set by the tuning conf). You reach it via an SSH
tunnel from your laptop:

```bash
ssh -N -L 5433:localhost:5432 ubuntu@<public-ip>
# now locally:
psql 'postgresql://nj:<pw>@localhost:5433/nj'
```

When you later serve the app, put Caddy (80/443) in front and open only
those ports — never 5432.

### 3. Bootstrap the substrate
SSH in, rotate the password, then run the one-command bootstrap:

```bash
ssh ubuntu@<public-ip>
sudo -u postgres psql -c "ALTER ROLE nj PASSWORD '<your-strong-pw>';"
cd /opt/nj
PG_DSN='postgresql://nj:<your-strong-pw>@localhost:5432/nj' \
  bash deploy/oracle/bootstrap.sh
```

`bootstrap.sh` is idempotent: venv + `pip install -e .`, `nj-migrate
apply`, `nj-migrate seed`, national CMS load (Part D + Part B, 2024+2023),
national LEIE, per-cycle refresh, then prints the validation snapshot.
Schema-only (skip the multi-GB load): prefix `NJ_SKIP_INGEST=1`.
Different years: `NJ_YEARS="2024 2023 2022"`.

After the bulk load:
```bash
psql "$PG_DSN" -c 'ANALYZE;'
```

### 4. (Optional) point the app at this box
The Next.js app reads `NEON_DATABASE_URL`. To serve national leads from
here instead of Neon, set that env var to this box's DSN **reached over a
private path** (Cloudflare Tunnel / Tailscale — keep 5432 off the public
internet). This is a deliberate prod cutover; not required for the data
work.

## Backups (OPS-5, when you cut over)
Nightly `pg_dump` → Cloudflare R2 (10 GB free, $0 egress) per `AGENTS.md`.
No point-in-time recovery yet; the raw substrate is also fully
reproducible by re-running the public ingesters.

## Files
- `cloud-init.yaml` — first-boot provisioning (PG16, Python 3.13, role/db,
  repo checkout, tuning conf).
- `postgresql.tuning.conf` — canonical tuning (kept in sync with the copy
  embedded in cloud-init).
- `bootstrap.sh` — idempotent schema + national-substrate loader.
