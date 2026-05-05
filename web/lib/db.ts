/**
 * Neon HTTP-based Postgres client.
 *
 * Why HTTP, not a TCP socket pool: Vercel's serverless functions have
 * sub-second cold-start budgets and short-lived execution contexts.
 * A long-lived connection pool (`pg.Pool`) is wrong shape:
 *   - cold starts pay socket-handshake latency on every request
 *   - the pool may outlive the function instance, leaking sockets
 *   - Postgres `max_connections` is capped, so a fanout of N concurrent
 *     functions can exhaust the DB even at low total RPS.
 *
 * `@neondatabase/serverless` issues each query as a single HTTPS POST
 * to Neon's edge proxy. Stateless, no pool, no sockets to leak.
 *
 * Required env: NEON_DATABASE_URL (exact string from Neon's "Connection
 * details -> Pooled connection -> psql" snippet).
 */

import { neon, type NeonQueryFunction } from "@neondatabase/serverless";

let _sql: NeonQueryFunction<false, false> | null = null;

export function getSql(): NeonQueryFunction<false, false> {
  if (_sql) return _sql;
  const url = process.env.NEON_DATABASE_URL;
  if (!url) {
    throw new Error(
      "NEON_DATABASE_URL is not set. " +
        "Set it in Vercel project env vars (or .env.local for `npm run dev`).",
    );
  }
  _sql = neon(url);
  return _sql;
}

/**
 * Truthy when NEON_DATABASE_URL is set and the canary query succeeds.
 * Used by /api/health and by every page-level fetch as a graceful
 * degradation guard so a misconfigured deployment serves an empty
 * state, not a 500.
 */
export async function isDbReachable(): Promise<{
  reachable: boolean;
  error?: string;
}> {
  if (!process.env.NEON_DATABASE_URL) {
    return { reachable: false, error: "NEON_DATABASE_URL not set" };
  }
  try {
    const sql = getSql();
    await sql`SELECT 1 AS ok`;
    return { reachable: true };
  } catch (err) {
    return {
      reachable: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}
