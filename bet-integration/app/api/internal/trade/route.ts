import { NextResponse } from "next/server";
import { z } from "zod";
import { db } from "@/lib/db";
import { checkInternalSecret } from "@/lib/internal-auth";
import { publish, Channels } from "@/lib/pubsub";
import { logger } from "@/lib/logger";
import { InsufficientFundsError } from "@/lib/wallet-safe";
import { executeBuy, executeSell, HttpError } from "@/lib/trade-core";

/**
 * Internal trade route — lets the bot-fleet simulator place trades AS a given
 * user without a NextAuth session, authenticated by the shared
 * INTERNAL_API_SECRET (same pattern as /api/internal/wallet). It reuses the
 * EXACT executeBuy/executeSell from lib/trade-core, so a bot trade is identical
 * to a human one (AMM math, fees, positions, achievements) and publishes the
 * same SSE events so bot activity shows up live in the UI.
 *
 *   POST /api/internal/trade
 *   Authorization: Bearer <INTERNAL_API_SECRET>
 *   { side:"BUY",  userId, marketId, outcome:"YES"|"NO", coins }
 *   { side:"SELL", userId, marketId, outcome:"YES"|"NO", shares }
 */
const Body = z.discriminatedUnion("side", [
  z.object({
    side: z.literal("BUY"),
    userId: z.string().min(1),
    marketId: z.string().min(1),
    outcome: z.enum(["YES", "NO"]),
    coins: z.number().int().min(1).max(1_000_000),
  }),
  z.object({
    side: z.literal("SELL"),
    userId: z.string().min(1),
    marketId: z.string().min(1),
    outcome: z.enum(["YES", "NO"]),
    shares: z.number().gt(0).max(1_000_000),
  }),
]);

export async function POST(req: Request) {
  const auth = checkInternalSecret(req);
  if (!auth.ok) {
    return NextResponse.json({ error: "unauthorized", reason: auth.reason }, { status: 401 });
  }

  const parsed = Body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json({ error: "invalid_input", details: parsed.error.flatten() }, { status: 400 });
  }
  const d = parsed.data;

  try {
    const result =
      d.side === "BUY"
        ? await executeBuy(d.userId, d.marketId, d.outcome, d.coins)
        : await executeSell(d.userId, d.marketId, d.outcome, d.shares);

    const user = await db.user.findUnique({ where: { id: d.userId }, select: { username: true } });
    const cost = d.side === "BUY" ? (result.trade as { cost: number }).cost : (result.trade as { coinsReceived: number }).coinsReceived;

    publish(Channels.market(result.market.id), {
      type: "trade",
      yesPrice: result.market.yesPrice,
      noPrice: result.market.noPrice,
      volumeCoins: result.market.volumeCoins,
      side: d.outcome,
      action: d.side,
      cost,
      at: Date.now(),
    });
    publish(Channels.global(), {
      type: "activity",
      marketId: result.market.id,
      marketTitle: result.market.title,
      marketSlug: result.market.slug,
      action: d.side,
      outcome: d.outcome,
      username: user?.username ?? "trader",
      coins: cost,
      shares: result.trade.shares,
      price: result.trade.avgPrice,
      at: Date.now(),
    });

    return NextResponse.json({ ok: true, ...result });
  } catch (e) {
    if (e instanceof HttpError) {
      return NextResponse.json({ error: e.message }, { status: e.status });
    }
    if (e instanceof InsufficientFundsError) {
      return NextResponse.json({ error: "insufficient_coins" }, { status: 400 });
    }
    logger.error(e, { route: "/api/internal/trade", userId: d.userId, marketId: d.marketId });
    return NextResponse.json({ error: "internal" }, { status: 500 });
  }
}
