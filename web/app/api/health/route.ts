import { NextResponse } from "next/server";

import { isDbReachable } from "@/lib/db";
import { resolveDefaultCycle } from "@/lib/queries";

export const dynamic = "force-dynamic";
export const runtime = "edge";

/**
 * GET /api/health
 *
 * Liveness/readiness probe for the screener. Returns 200 with
 * { db_reachable, cycle_default } when the configured Neon DSN
 * accepts queries; 503 otherwise. The Vercel deployment sets
 * cache-control: no-store on this route via the response header
 * (route segments default to per-request execution under
 * `force-dynamic`).
 */
export async function GET() {
  const t0 = Date.now();
  const r = await isDbReachable();
  if (!r.reachable) {
    return NextResponse.json(
      {
        ok: false,
        db_reachable: false,
        error: r.error ?? "unknown",
        latency_ms: Date.now() - t0,
      },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }

  let cycle: string | null = null;
  try {
    cycle = await resolveDefaultCycle();
  } catch {
    // Schema absent -> still healthy at the DB level.
  }

  return NextResponse.json(
    {
      ok: true,
      db_reachable: true,
      cycle_default: cycle,
      latency_ms: Date.now() - t0,
    },
    { headers: { "cache-control": "no-store" } },
  );
}
