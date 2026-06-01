/**
 * Core trade execution — shared by the public `POST /api/trade` (session auth)
 * and the internal `POST /api/internal/trade` (Bearer service auth, used by the
 * bot-fleet simulator). Both call the SAME `executeBuy`/`executeSell` so a bot
 * trade is byte-for-byte identical to a human one: same AMM math, same fee
 * split, same position/transaction/price-point writes, same achievement hooks.
 *
 * Extracted verbatim from the original `app/api/trade/route.ts` — the only
 * change is that these functions are now exported and parameterised purely by
 * `userId` (they never read the session).
 */
import { db } from "@/lib/db";
import { quoteBuy, quoteSell, chargeForCoins } from "@/lib/amm";
import { onTrade } from "@/lib/achievements";
import { splitBuy, splitSell } from "@/lib/commission";
import { collectFee } from "@/lib/house";
import { safeDebit } from "@/lib/wallet-safe";
import type { Outcome } from "@prisma/client";

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function executeBuy(
  userId: string,
  marketId: string,
  outcome: "YES" | "NO",
  coins: number,
) {
  return db.$transaction(async (tx) => {
    const market = await tx.market.findUnique({ where: { id: marketId } });
    if (!market) throw new HttpError(404, "market_not_found");
    if (market.status !== "OPEN") throw new HttpError(409, "market_not_open");
    if (market.endsAt.getTime() <= Date.now()) {
      throw new HttpError(409, "market_ended");
    }

    const wallet = await tx.wallet.findUnique({ where: { userId } });
    if (!wallet) throw new HttpError(404, "wallet_missing");
    if (wallet.balance < coins) throw new HttpError(400, "insufficient_coins");

    // Skim the platform fee FIRST. The AMM then quotes against the net
    // amount, so the user's shares are proportional to what actually
    // entered the pool — the fee never sees the pool, so it can't move
    // the price for other traders.
    const { netCoins, fee } = splitBuy(coins);
    if (netCoins <= 0) throw new HttpError(400, "below_min_after_fee");
    const quote = quoteBuy(
      { yesShares: market.yesShares, noShares: market.noShares },
      outcome,
      netCoins,
    );
    if (!quote) throw new HttpError(400, "quote_failed");
    const charge = chargeForCoins(coins);
    const poolCharge = chargeForCoins(netCoins);

    // Atomic debit guarded against negative balance.
    await safeDebit(tx, userId, charge);

    const updatedMarket = await tx.market.update({
      where: { id: market.id },
      data: {
        yesShares: quote.newReserves.yesShares,
        noShares: quote.newReserves.noShares,
        volumeCoins: { increment: charge },
        trendingScore: { increment: charge },
      },
    });

    const trade = await tx.trade.create({
      data: {
        marketId: market.id,
        userId,
        outcome: outcome as Outcome,
        shares: quote.sharesOut,
        cost: poolCharge,
        feeCoins: fee,
        pricePerShare: quote.avgPrice,
        yesSharesAfter: quote.newReserves.yesShares,
        noSharesAfter: quote.newReserves.noShares,
      },
    });

    await tx.position.upsert({
      where: {
        userId_marketId_outcome: { userId, marketId: market.id, outcome: outcome as Outcome },
      },
      create: {
        userId,
        marketId: market.id,
        outcome: outcome as Outcome,
        shares: quote.sharesOut,
        costBasis: poolCharge,
      },
      update: {
        shares: { increment: quote.sharesOut },
        costBasis: { increment: poolCharge },
      },
    });

    await tx.transaction.create({
      data: {
        userId,
        delta: -charge,
        kind: "trade_buy",
        reference: trade.id,
        metadata: { marketId: market.id, outcome, shares: quote.sharesOut, fee },
      },
    });

    await collectFee(tx, {
      amount: fee,
      kind: "commission_buy",
      reference: `buy:${trade.id}`,
      metadata: { marketId: market.id, outcome, takerId: userId },
    });

    await tx.pricePoint.create({
      data: {
        marketId: market.id,
        yesPrice: quote.newYesPrice,
        noPrice: 1 - quote.newYesPrice,
      },
    });

    const xp = Math.min(50, Math.max(1, Math.floor(charge / 20)));
    await tx.user.update({ where: { id: userId }, data: { xp: { increment: xp } } });
    const unlocks = await onTrade(tx, userId, { coinsSpent: charge });

    return {
      trade: {
        id: trade.id,
        shares: quote.sharesOut,
        cost: charge,
        avgPrice: quote.avgPrice,
      },
      market: {
        id: updatedMarket.id,
        title: updatedMarket.title,
        slug: updatedMarket.slug,
        yesPrice: quote.newYesPrice,
        noPrice: 1 - quote.newYesPrice,
        volumeCoins: updatedMarket.volumeCoins,
      },
      // Wallet balance after the debit — the internal route surfaces this so
      // the bot runner can update its local balance cache without a re-fetch.
      balanceAfter: wallet.balance - charge,
      xpAwarded: xp,
      unlocks,
    };
  });
}

export async function executeSell(
  userId: string,
  marketId: string,
  outcome: "YES" | "NO",
  shares: number,
) {
  return db.$transaction(async (tx) => {
    const market = await tx.market.findUnique({ where: { id: marketId } });
    if (!market) throw new HttpError(404, "market_not_found");
    if (market.status !== "OPEN") throw new HttpError(409, "market_not_open");
    if (market.endsAt.getTime() <= Date.now()) {
      throw new HttpError(409, "market_ended");
    }

    const pos = await tx.position.findUnique({
      where: {
        userId_marketId_outcome: { userId, marketId: market.id, outcome: outcome as Outcome },
      },
    });
    if (!pos) throw new HttpError(400, "insufficient_shares");
    const available = pos.shares - pos.locked;
    if (available + 1e-9 < shares) throw new HttpError(400, "insufficient_shares");

    const quote = quoteSell(
      { yesShares: market.yesShares, noShares: market.noShares },
      outcome,
      shares,
    );
    if (!quote) throw new HttpError(400, "quote_failed");

    const gross = Math.floor(quote.coinsOut);
    if (gross <= 0) throw new HttpError(400, "quote_failed");
    const { netCoins: coinsReceived, fee } = splitSell(gross);
    if (coinsReceived <= 0) throw new HttpError(400, "below_min_after_fee");

    const updatedWallet = await tx.wallet.update({
      where: { userId },
      data: { balance: { increment: coinsReceived } },
    });

    const updatedMarket = await tx.market.update({
      where: { id: market.id },
      data: {
        yesShares: quote.newReserves.yesShares,
        noShares: quote.newReserves.noShares,
        volumeCoins: { increment: gross },
        trendingScore: { increment: gross },
      },
    });

    const trade = await tx.trade.create({
      data: {
        marketId: market.id,
        userId,
        outcome: outcome as Outcome,
        shares,
        cost: -coinsReceived,
        feeCoins: fee,
        pricePerShare: quote.avgPrice,
        yesSharesAfter: quote.newReserves.yesShares,
        noSharesAfter: quote.newReserves.noShares,
      },
    });

    const sharesBefore = pos.shares;
    const ratio = (sharesBefore - shares) / sharesBefore;
    await tx.position.update({
      where: { id: pos.id },
      data: {
        shares: { decrement: shares },
        costBasis: Math.max(0, Math.floor(pos.costBasis * ratio)),
        realizedPnl: { increment: coinsReceived - Math.round(pos.costBasis * (1 - ratio)) },
      },
    });

    await tx.transaction.create({
      data: {
        userId,
        delta: coinsReceived,
        kind: "trade_sell",
        reference: trade.id,
        metadata: { marketId: market.id, outcome, shares, fee, gross },
      },
    });

    await collectFee(tx, {
      amount: fee,
      kind: "commission_sell",
      reference: `sell:${trade.id}`,
      metadata: { marketId: market.id, outcome, takerId: userId },
    });

    await tx.pricePoint.create({
      data: {
        marketId: market.id,
        yesPrice: quote.newYesPrice,
        noPrice: 1 - quote.newYesPrice,
      },
    });

    const xp = Math.min(50, Math.max(1, Math.floor(coinsReceived / 20)));
    await tx.user.update({ where: { id: userId }, data: { xp: { increment: xp } } });
    const unlocks = await onTrade(tx, userId, { coinsSpent: coinsReceived });

    return {
      trade: {
        id: trade.id,
        shares,
        coinsReceived,
        avgPrice: quote.avgPrice,
      },
      market: {
        id: updatedMarket.id,
        title: updatedMarket.title,
        slug: updatedMarket.slug,
        yesPrice: quote.newYesPrice,
        noPrice: 1 - quote.newYesPrice,
        volumeCoins: updatedMarket.volumeCoins,
      },
      balanceAfter: updatedWallet.balance,
      xpAwarded: xp,
      unlocks,
    };
  });
}
