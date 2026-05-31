import { sql } from "kysely";
import { getDb } from "../db/client.js";
import type { TranscriptSegmentRow } from "../db/types.js";
import { iso } from "./ids.js";

export type TranscriptSearchOptions = {
  organizationId: string;
  actorUserId?: string | null;
  query?: string | null;
  sessionId?: string | null;
  deviceId?: string | null;
  from?: Date | null;
  to?: Date | null;
  before?: Date | null;
  limit: number;
};

type PublicSearchSegment = {
  id: string;
  userId: string;
  deviceId: string | null;
  sessionId: string;
  source: string;
  text: string;
  words: unknown;
  isInterim: boolean;
  speakerLabel: string | null;
  speakerUserId: string | null;
  speakerName: string | null;
  speakerConfidence: number | null;
  confidence: number | null;
  startedAt: string;
  endedAt: string | null;
  createdAt: string;
  score: number | null;
};

function escapeLike(value: string) {
  return value.replace(/[\\%_]/g, (match) => `\\${match}`);
}

function normalizeQuery(value: string | null | undefined) {
  return value?.replace(/\s+/g, " ").trim().toLowerCase() ?? "";
}

function toPublicSegment(row: TranscriptSegmentRow, score: number | null): PublicSearchSegment {
  return {
    id: row.id,
    userId: row.user_id,
    deviceId: row.device_id,
    sessionId: row.session_id,
    source: row.source,
    text: row.text,
    words: row.words,
    isInterim: row.is_interim,
    speakerLabel: row.speaker_label,
    speakerUserId: row.speaker_user_id,
    speakerName: row.speaker_name,
    speakerConfidence: row.speaker_confidence,
    confidence: row.confidence,
    startedAt: iso(row.started_at) ?? "",
    endedAt: iso(row.ended_at),
    createdAt: iso(row.created_at) ?? "",
    score,
  };
}

export async function indexTranscriptSegment(_segment: TranscriptSegmentRow) {
  // Local search reads transcript_segments directly. SQLite FTS5 will replace
  // this no-op with local index maintenance when transcript volume requires it.
}

export async function searchTranscriptIndex(options: TranscriptSearchOptions) {
  const queryText = normalizeQuery(options.query);
  const likeQuery = queryText ? `%${escapeLike(queryText)}%` : null;
  const prefixQuery = queryText ? `${escapeLike(queryText)}%` : null;

  let query = getDb()
    .selectFrom("transcript_segments")
    .selectAll()
    .where("organization_id", "=", options.organizationId)
    .where("is_interim", "=", false)
    .limit(options.limit);

  if (options.sessionId) query = query.where("session_id", "=", options.sessionId);
  if (options.deviceId) query = query.where("device_id", "=", options.deviceId);
  if (options.from) query = query.where("started_at", ">=", options.from);
  if (options.to) query = query.where("started_at", "<", options.to);
  if (options.before) query = query.where("started_at", "<", options.before);
  if (likeQuery) {
    query = query.where(sql<boolean>`lower(text) like ${likeQuery} escape '\\'`);
    query = query.orderBy(
      sql<number>`case
        when lower(text) = ${queryText} then 0
        when lower(text) like ${prefixQuery} escape '\\' then 1
        else 2
      end`,
    );
  }
  query = query.orderBy("started_at", "desc");

  const rows = await query.execute();
  return {
    segments: rows.map((row, index) => toPublicSegment(row, likeQuery ? 1 / (index + 1) : null)),
    facets: await transcriptFacets(options),
  };
}

export async function transcriptFacets(options: TranscriptSearchOptions) {
  let query = getDb()
    .selectFrom("transcript_segments")
    .select(({ fn }) => [
      "device_id as deviceId",
      "session_id as sessionId",
      "speaker_label as speakerLabel",
      fn.countAll<string>().as("count"),
    ])
    .where("organization_id", "=", options.organizationId)
    .where("is_interim", "=", false)
    .groupBy(["device_id", "session_id", "speaker_label"])
    .limit(100);

  if (options.sessionId) query = query.where("session_id", "=", options.sessionId);
  if (options.deviceId) query = query.where("device_id", "=", options.deviceId);
  if (options.from) query = query.where("started_at", ">=", options.from);
  if (options.to) query = query.where("started_at", "<", options.to);
  if (options.before) query = query.where("started_at", "<", options.before);

  return query.execute();
}
