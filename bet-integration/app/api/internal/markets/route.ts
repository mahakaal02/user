import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { checkInternalSecret } from "@/lib/internal-auth";

/**
 * Internal market list — every OPEN market with its current AMM state, so the
 * bot fleet can poll for "existing + NEW" markets and quote prices locally.
 *
 *   GET /api/internal/markets
 *   Authorization: Bearer <INTERNAL_API_SECRET>
 *   → { markets: [{ id, slug, title, category, yesPrice, yesShares, noShares, endsAt, volumeCoins }], count }
 */
export async function GET(req: Request) {
  const auth = checkInternalSecret(req);
  if (!auth.ok) {
    return NextResponse.json({ error: "unauthorized", reason: auth.reason }, { status: 401 });
  }

  const rows = await db.market.findMany({
    where: { status: "OPEN" },
    select: {
      id: true,
      slug: true,
      title: true,
      category: true,
      yesShares: true,
      noShares: true,
      endsAt: true,
      volumeCoins: true,
      createdAt: true,
    },
    orderBy: { createdAt: "desc" },
    take: 1000,
  });

  const markets = rows.map((m) => {
    const total = m.yesShares + m.noShares;
    return {
      id: m.id,
      slug: m.slug,
      title: m.title,
      category: m.category,
      yesPrice: total > 0 ? m.noShares / total : 0.5,
      yesShares: m.yesShares,
      noShares: m.noShares,
      endsAt: m.endsAt,
      volumeCoins: m.volumeCoins,
      createdAt: m.createdAt,
    };
  });

  return NextResponse.json({ markets, count: markets.length });
}
