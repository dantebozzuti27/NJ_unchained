# Platform constraints (pinned)

These are durable constraints, not one-off requests. Every design choice
must respect them; deviating requires explicit user approval.

## Operating cost: $0/month, indefinitely

The platform must run end-to-end at zero monthly spend. No paid services
in defaults, no card-charging tiers, no "free trial then bills you" SaaS.
Free-tier services are fine; pay-as-you-go services are not, unless the
user explicitly authorizes the line item.

This applies to:

- Compute, hosting, networking
- Managed databases
- Object storage
- Notifications (Slack/email/SMS)
- LLM APIs
- Monitoring / log aggregation
- Domain registration

If a feature genuinely cannot ship without paid infrastructure, surface
the cost trade-off explicitly and ask before building. Do not silently
default to a paid service.

## Production deployment target: Oracle Cloud Always Free

When the platform moves off localhost, the target is a single
**VM.Standard.A1.Flex** (4 OCPU ARM Ampere, 24 GB RAM, 200 GB block
storage) on Oracle Cloud's Always Free tier. Self-host everything on
that one box: Postgres, Dagster (web + daemon), FastAPI, Caddy/TLS.

Migrate off only if the user explicitly authorizes paid hosting.

## Free-tier defaults for future features

Pin these as the default choices when the matching feature lands:

| Feature                           | Default                                          |
|-----------------------------------|--------------------------------------------------|
| Notifications (BBG-LIKE-4)        | Discord webhook or Slack free workspace webhook  |
| LLM (Tier 5 NL surface)           | Local Ollama (Llama 3.3 70B / Qwen 2.5 Coder)    |
|                                   | OR Groq free-tier OR Google AI Studio free tier  |
| Object storage / backups (OPS-5)  | Cloudflare R2 (10 GB free, $0 egress)            |
| TLS                               | Caddy + Let's Encrypt                            |
| CI                                | GitHub Actions free tier                         |
| Monitoring (optional)             | BetterStack uptime free tier; Sentry free tier   |
| Auth on read API (if needed)      | Cloudflare Tunnel + Zero Trust (free up to 50)   |

## Free public-data API keys to register

The platform reads from these data sources. All public, all free.

| Key                | Source                                                          | Required? |
|--------------------|-----------------------------------------------------------------|-----------|
| `CENSUS_API_KEY`   | <https://api.census.gov/data/key_signup.html>                  | Optional (rate-limit upgrade) |
| `BLS_API_KEY`      | <https://www.bls.gov/registrationEngine/>                       | Optional (rate-limit upgrade) |
| HUD User login     | <https://www.huduser.gov/apps/public/usps/register>             | Required to download crosswalk |
| SAM.gov public key | <https://sam.gov> -> "Get a Public API Key" (when FRAUD-F2 lands) | Required for some endpoints |

All other data sources (FRED, FHFA, NJ DCA, FEC bulk, NJ ELEC, NJ
YourMoney, NJ DOE, USAspending, FAPIIS, SBA PPP, HHS-OIG LEIE, GAO, CMS
Medicare, IPEDS, ED Data Express, Senate LDA, GLEIF, Census ACS PUMS)
are public and keyless.

## What this implies for design

- Postgres tuning targets a single ~24 GB-RAM VM, not horizontal scale.
- LLM-dependent features must degrade gracefully on local Llama quality.
- Storage growth is bounded to ~100 GB; aggressive partitioning isn't
  needed yet, but plan for it as cross-cycle FEC data lands.
- Backups are nightly `pg_dump` -> R2; no point-in-time recovery yet.
- The platform stays single-tenant. Multi-tenancy requires the user to
  authorize a different deployment story.
