import { HTTPException } from "hono/http-exception";
import { sql } from "kysely";
import { getDb, isSqlite } from "../db/client.js";
import type { SummaryRow, TranscriptSegmentRow } from "../db/types.js";
import type { UserContext } from "./auth.js";
import { id, iso, now } from "./ids.js";

export function dateFrom(value: string | number | Date) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    throw new HTTPException(400, { message: "Invalid date" });
  }
  return date;
}

function jsonb(value: unknown) {
  if (isSqlite()) return sql<unknown>`${JSON.stringify(value)}`;
  return sql<unknown>`cast(${JSON.stringify(value)} as jsonb)`;
}

function jsonArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

export function publicSummary(summary: SummaryRow) {
  return {
    id: summary.id,
    type: summary.type,
    title: summary.title,
    summary: summary.summary,
    importantPoints: jsonArray(summary.important_points),
    actionItems: jsonArray(summary.action_items),
    sourceSegmentIds: jsonArray(summary.source_segment_ids),
    periodStart: iso(summary.period_start),
    periodEnd: iso(summary.period_end),
    status: summary.status,
    generatedAt: iso(summary.generated_at),
    createdAt: iso(summary.created_at),
    updatedAt: iso(summary.updated_at),
  };
}

function compactLine(text: string) {
  return text.replace(/\s+/g, " ").trim();
}

function uniqueLines(lines: string[], limit: number) {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const line of lines) {
    const normalized = compactLine(line);
    if (!normalized || normalized.length < 8) continue;
    const key = normalized.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(normalized.length > 220 ? `${normalized.slice(0, 217).trim()}...` : normalized);
    if (result.length >= limit) break;
  }
  return result;
}

function looksImportant(text: string) {
  return /\b(decided|decision|important|need to|todo|to do|follow up|follow-up|send|email|call|schedule|remember|deadline|launch|ship|fix|bug|issue|blocked|next|plan|should|will)\b/i.test(text);
}

function dailyOverview(lines: string[], importantPoints: string[], actionItems: string[]) {
  if (!lines.length) return "Nothing for this day yet.";
  if (actionItems.length && importantPoints.length) {
    return `Main themes from the day are captured below, with ${actionItems.length} action ${actionItems.length === 1 ? "item" : "items"} pulled out.`;
  }
  if (actionItems.length) {
    return `${actionItems.length} action ${actionItems.length === 1 ? "item" : "items"} found from today's conversations.`;
  }
  if (importantPoints.length >= 3) {
    return "Key points from today's conversations are captured below.";
  }
  return "Brief notes from today are below.";
}

function buildDailySummary(segments: TranscriptSegmentRow[], periodStart: Date) {
  const lines = segments.map((segment) => compactLine(segment.text)).filter(Boolean);
  const importantPoints = uniqueLines(
    [
      ...lines.filter(looksImportant),
      ...lines.filter((line) => line.length >= 80),
      ...lines,
    ],
    12,
  );
  const actionItems = uniqueLines(
    lines.filter((line) => /\b(need to|todo|to do|follow up|follow-up|send|email|call|schedule|remember|remind|should|will)\b/i.test(line)),
    10,
  );
  const title = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(periodStart);

  if (!segments.length) {
    return {
      title,
      summary: "Nothing for this day yet.",
      importantPoints: [],
      actionItems: [],
    };
  }

  return {
    title,
    summary: dailyOverview(lines, importantPoints, actionItems),
    importantPoints,
    actionItems,
  };
}

export async function generateDailySummaryForUser(options: {
  user: UserContext;
  periodStart: Date;
  periodEnd: Date;
}) {
  const { user, periodStart, periodEnd } = options;
  if (periodEnd.getTime() <= periodStart.getTime()) {
    throw new HTTPException(400, { message: "periodEnd must be after periodStart" });
  }

  const segments = await getDb()
    .selectFrom("transcript_segments")
    .selectAll()
    .where("organization_id", "=", user.organizationId)
    .where("user_id", "=", user.userId)
    .where("is_interim", "=", false)
    .where("started_at", ">=", periodStart)
    .where("started_at", "<", periodEnd)
    .orderBy("started_at", "asc")
    .limit(1200)
    .execute();
  const generated = buildDailySummary(segments, periodStart);
  const date = now();
  const summary = await getDb()
    .insertInto("summaries")
    .values({
      id: id("sum"),
      organization_id: user.organizationId,
      user_id: user.userId,
      type: "daily",
      title: generated.title,
      summary: generated.summary,
      important_points: jsonb(generated.importantPoints),
      action_items: jsonb(generated.actionItems),
      source_segment_ids: jsonb(segments.map((segment) => segment.id)),
      period_start: periodStart,
      period_end: periodEnd,
      status: "ready",
      generated_at: date,
      created_at: date,
      updated_at: date,
    })
    .onConflict((oc) =>
      oc.columns(["organization_id", "user_id", "type", "period_start", "period_end"]).doUpdateSet({
        title: generated.title,
        summary: generated.summary,
        important_points: jsonb(generated.importantPoints),
        action_items: jsonb(generated.actionItems),
        source_segment_ids: jsonb(segments.map((segment) => segment.id)),
        status: "ready",
        generated_at: date,
        updated_at: date,
      }),
    )
    .returningAll()
    .executeTakeFirstOrThrow();

  return { summary, segmentCount: segments.length };
}
