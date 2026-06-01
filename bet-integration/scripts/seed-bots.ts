/**
 * Seed bot-fleet users for the agent-based simulation.
 *
 * Creates N human-named users (e.g. `prashant_jio`, `riya.kapoor`) each with a
 * 20,000-coin wallet, tagged by the `@sim.kalki.local` email domain so the
 * fleet can be listed (GET /api/internal/bot-users) and purged. The usernames
 * are deliberately human-like (closed research sandbox).
 *
 * Idempotent: re-running tops the roster up to N without resetting existing
 * balances. Pass --reset-coins to force every bot wallet back to 20,000.
 *
 *   BOTS_COUNT=1000 npx tsx scripts/seed-bots.ts
 *   npx tsx scripts/seed-bots.ts --count 200 --reset-coins
 *   npx tsx scripts/seed-bots.ts --purge          # delete the whole fleet
 */
import { Prisma, PrismaClient } from "@prisma/client";

// Plain `tsx` (unlike the Prisma CLI) doesn't auto-load .env — load it BEFORE
// constructing the client so PrismaClient finds DATABASE_URL. Node >= 20.12.
try {
  (process as unknown as { loadEnvFile?: () => void }).loadEnvFile?.();
} catch {
  /* env already set, or older Node — ignore */
}

const db = new PrismaClient();

const DOMAIN = "@sim.kalki.local";
const START_COINS = 20_000;

const FIRST = [
  "prashant", "riya", "arjun", "neha", "rohit", "ananya", "vikram", "sneha", "karan", "pooja",
  "amit", "divya", "rahul", "isha", "sandeep", "megha", "nikhil", "priya", "aakash", "tanvi",
  "harsh", "kavya", "manish", "shreya", "gaurav", "ritika", "aditya", "simran", "varun", "nidhi",
  "yash", "payal", "dev", "anjali", "siddharth", "kritika", "raj", "swati", "abhishek", "pallavi",
];
const LAST = [
  "sharma", "verma", "kapoor", "gupta", "singh", "reddy", "nair", "iyer", "mehta", "jain",
  "khanna", "bose", "das", "rao", "patel", "malik", "chopra", "sethi", "bhat", "pillai",
];
const STYLES: ((f: string, l: string, n: number) => string)[] = [
  (f, l) => `${f}_${l}`, (f, l) => `${f}.${l}`, (f, l, n) => `${f}_${l}${n}`,
  (f, _l, n) => `${f}${n}`, (f) => `${f}_jio`, (f) => `${f}_trades`,
  (f) => `the_${f}`, (f, _l, n) => `${f}_${n}`, (f, l) => `${f}${l}`, (f) => `real_${f}`,
];

function* usernames(): Generator<string> {
  // Deterministic LCG so re-runs produce the SAME roster (stable emails → idempotent upserts).
  let seed = 1234567;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const seen = new Set<string>();
  while (true) {
    const f = FIRST[Math.floor(rnd() * FIRST.length)];
    const l = LAST[Math.floor(rnd() * LAST.length)];
    const n = Math.floor(rnd() * 90) + 10;
    const style = STYLES[Math.floor(rnd() * STYLES.length)];
    let u = style(f, l, n).toLowerCase();
    if (seen.has(u)) u = `${u}${seen.size}`;
    if (seen.has(u)) continue;
    seen.add(u);
    yield u;
  }
}

async function purge() {
  const res = await db.user.deleteMany({ where: { email: { endsWith: DOMAIN } } });
  console.log(`Purged ${res.count} bot users (cascade removed wallets/positions/comments).`);
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--purge")) {
    await purge();
    await db.$disconnect();
    return;
  }
  const countArg = args.indexOf("--count");
  const count = countArg >= 0 ? parseInt(args[countArg + 1], 10) : parseInt(process.env.BOTS_COUNT ?? "1000", 10);
  const resetCoins = args.includes("--reset-coins");

  console.log(`Seeding ${count} bot users (${START_COINS} coins each)${resetCoins ? " [reset coins]" : ""}…`);
  const gen = usernames();
  let created = 0;
  let existing = 0;

  for (let i = 0; i < count; i++) {
    let username = gen.next().value as string;
    const email = `${username}${DOMAIN}`;

    // Create the user (handle the rare username collision with a non-bot row).
    let user;
    try {
      user = await db.user.upsert({
        where: { email },
        update: {},
        create: { email, username, emailVerified: true },
      });
    } catch (e) {
      if (e instanceof Prisma.PrismaClientKnownRequestError && e.code === "P2002") {
        username = `${username}_${i}`;
        user = await db.user.upsert({
          where: { email },
          update: {},
          create: { email, username, emailVerified: true },
        });
      } else {
        throw e;
      }
    }

    const wallet = await db.wallet.findUnique({ where: { userId: user.id } });
    if (!wallet) {
      await db.wallet.create({ data: { userId: user.id, balance: START_COINS } });
      created++;
    } else {
      if (resetCoins) {
        await db.wallet.update({ where: { userId: user.id }, data: { balance: START_COINS } });
      }
      existing++;
    }
    if ((i + 1) % 100 === 0) console.log(`  …${i + 1}/${count}`);
  }

  const total = await db.user.count({ where: { email: { endsWith: DOMAIN } } });
  console.log(`Done. created=${created} existing=${existing} | total bot users=${total}`);
  await db.$disconnect();
}

main().catch(async (e) => {
  console.error(e);
  await db.$disconnect();
  process.exit(1);
});
