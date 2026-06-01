import { NextResponse } from "next/server";
import { z } from "zod";
import { db } from "@/lib/db";
import { checkInternalSecret } from "@/lib/internal-auth";

/**
 * Internal comment route — posts a comment AS a given user, authenticated by
 * INTERNAL_API_SECRET. Reuses the same Comment shape as the public comments
 * route (single-level threading, 500-char cap). Used by the bot fleet to post
 * LLM-generated one-liners ("buying YES here, momentum looks strong").
 *
 *   POST /api/internal/comment
 *   Authorization: Bearer <INTERNAL_API_SECRET>
 *   { userId, market: "<id|slug>", body, parentId? }
 */
const Body = z.object({
  userId: z.string().min(1),
  market: z.string().min(1),
  body: z.string().min(1).max(500),
  parentId: z.string().optional(),
});

export async function POST(req: Request) {
  const auth = checkInternalSecret(req);
  if (!auth.ok) {
    return NextResponse.json({ error: "unauthorized", reason: auth.reason }, { status: 401 });
  }

  const parsed = Body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json({ error: "invalid_input" }, { status: 400 });
  }
  const { userId, market: marketRef, body, parentId } = parsed.data;

  const market = await db.market.findFirst({
    where: { OR: [{ id: marketRef }, { slug: marketRef }] },
    select: { id: true },
  });
  if (!market) return NextResponse.json({ error: "not_found" }, { status: 404 });

  let resolvedParent: string | null = null;
  if (parentId) {
    const parent = await db.comment.findFirst({
      where: { id: parentId, marketId: market.id },
      select: { id: true, parentId: true },
    });
    if (parent) resolvedParent = parent.parentId ?? parent.id;
  }

  const c = await db.comment.create({
    data: { marketId: market.id, userId, body: body.trim(), parentId: resolvedParent },
  });
  return NextResponse.json({ ok: true, id: c.id });
}
