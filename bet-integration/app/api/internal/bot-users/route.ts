import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { checkInternalSecret } from "@/lib/internal-auth";

/**
 * Internal bot-fleet roster — the seeded simulation users (identified by the
 * @sim.kalki.local email domain) with their current balance. The Python runner
 * fetches this to know which users to trade as; it assigns each a personality
 * deterministically by hashing the userId, so no schema change is needed.
 *
 *   GET /api/internal/bot-users
 *   Authorization: Bearer <INTERNAL_API_SECRET>
 *   → { users: [{ id, username, balance }], count }
 */
const BOT_EMAIL_DOMAIN = "@sim.kalki.local";

export async function GET(req: Request) {
  const auth = checkInternalSecret(req);
  if (!auth.ok) {
    return NextResponse.json({ error: "unauthorized", reason: auth.reason }, { status: 401 });
  }

  const rows = await db.user.findMany({
    where: { email: { endsWith: BOT_EMAIL_DOMAIN } },
    select: { id: true, username: true, wallet: { select: { balance: true } } },
    orderBy: { createdAt: "asc" },
  });

  const users = rows.map((u) => ({
    id: u.id,
    username: u.username,
    balance: u.wallet?.balance ?? 0,
  }));
  return NextResponse.json({ users, count: users.length });
}
