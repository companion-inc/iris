import { Hono } from "hono";
import { z } from "zod";
import { getDb } from "../db/client.js";
import { requireUser } from "../lib/auth.js";
import { iso } from "../lib/ids.js";
import { dateFrom, generateDailySummaryForUser, publicSummary } from "../lib/summaries.js";

const dailySummarySchema = z.object({
  periodStart: z.union([z.string(), z.number(), z.date()]),
  periodEnd: z.union([z.string(), z.number(), z.date()]),
});

function cursorDate(value: string | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pageLimit(value: string | undefined, fallback = 30, max = 100) {
  if (!value) return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.min(max, Math.round(parsed)));
}

export const summaryRoutes = new Hono()
  .get("/v1/summaries", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const type = c.req.query("type") || "daily";
    const limit = pageLimit(c.req.query("limit"));
    const before = cursorDate(c.req.query("before"));
    let query = getDb()
      .selectFrom("summaries")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where("user_id", "=", user.userId)
      .where("type", "=", type)
      .orderBy("period_start", "desc")
      .limit(limit + 1);
    if (before) query = query.where("period_start", "<", before);
    const rows = await query.execute();
    const summaries = rows.slice(0, limit);
    return c.json({
      summaries: summaries.map(publicSummary),
      nextCursor: rows.length > limit ? iso(summaries[summaries.length - 1]?.period_start) : null,
    });
  })
  .post("/v1/summaries/daily", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = dailySummarySchema.parse(await c.req.json());
    const periodStart = dateFrom(body.periodStart);
    const periodEnd = dateFrom(body.periodEnd);
    const { summary, segmentCount } = await generateDailySummaryForUser({ user, periodStart, periodEnd });

    return c.json({ summary: publicSummary(summary), segmentCount });
  });
