import { Hono } from "hono";
import { sql } from "kysely";
import { z } from "zod";
import { HTTPException } from "hono/http-exception";
import { loadConfig } from "../config.js";
import { getDb, isSqlite } from "../db/client.js";
import { requireUser } from "../lib/auth.js";
import { id, iso, now } from "../lib/ids.js";
import { publishOrganization } from "../lib/events.js";
import { logInfo, logWarn, redactId } from "../lib/log.js";
import { publicSegment, publicVoiceSession } from "../lib/shape.js";
import { accountDisplayName } from "../lib/speaker-profiles.js";
import { indexTranscriptSegment, searchTranscriptIndex } from "../lib/transcript-search.js";
import type { TranscriptSegmentRow } from "../db/types.js";

const transcriptEventSchema = z.object({
  userId: z.string().trim().min(1).optional(),
  organizationId: z.string().trim().min(1).optional(),
  deviceId: z.string().trim().min(1).nullable().optional(),
  sessionId: z.string().trim().min(1),
  segmentId: z.string().trim().min(1).optional(),
  transcript: z.string().trim().min(1),
  words: z
    .array(
      z.object({
        text: z.string().trim().min(1),
        start: z.number().nullable().optional(),
        end: z.number().nullable().optional(),
        speaker: z.number().int().nullable().optional(),
      }),
    )
    .optional(),
  isFinal: z.boolean(),
  speakerId: z.string().nullable().optional(),
  speakerUserId: z.string().trim().min(1).nullable().optional(),
  speakerConfidence: z.number().min(0).max(1).nullable().optional(),
  emotionLabel: z.string().trim().min(1).nullable().optional(),
  emotionConfidence: z.number().min(0).max(1).nullable().optional(),
  emotionModel: z.string().trim().min(1).nullable().optional(),
  source: z.enum(["device", "assistant", "sound_recognition"]).default("device"),
  confidence: z.number().nullable().optional(),
  language: z.string().nullable().optional(),
  startedAt: z.union([z.string(), z.number(), z.date()]).optional(),
  endedAt: z.union([z.string(), z.number(), z.date()]).optional(),
});

function dateFrom(value: string | number | Date | undefined) {
  if (value === undefined) return now();
  return new Date(value);
}

function pageLimit(value: string | undefined, fallback = 50, max = 100) {
  if (!value) return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.min(max, Math.round(parsed)));
}

function cursorDate(value: string | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function searchText(value: string | undefined) {
  const text = value?.replace(/\s+/g, " ").trim();
  return text ? text.slice(0, 200) : null;
}

function serviceSearchContext(authorization: string | undefined, query: (name: string) => string | undefined) {
  const serviceApiKey = loadConfig().serviceApiKey;
  if (!serviceApiKey || authorization !== `Bearer ${serviceApiKey}`) return null;
  const userId = query("userId")?.trim();
  const organizationId = query("organizationId")?.trim();
  if (!userId || !organizationId) {
    throw new HTTPException(400, { message: "userId and organizationId are required" });
  }
  return { userId, organizationId };
}

function jsonb(value: unknown) {
  if (isSqlite()) return sql<unknown>`${JSON.stringify(value)}`;
  return sql<unknown>`cast(${JSON.stringify(value)} as jsonb)`;
}

async function speakerUsersForSegments(
  organizationId: string,
  segments: Array<{ speaker_user_id: string | null }>,
) {
  const speakerUserIds = [
    ...new Set(segments.map((segment) => segment.speaker_user_id).filter((id): id is string => Boolean(id))),
  ];
  return speakerUsersForIds(organizationId, speakerUserIds);
}

async function speakerUsersForIds(organizationId: string, speakerUserIds: string[]) {
  if (!speakerUserIds.length) return new Map<string, { name: string | null; email: string }>();
  const rows = await getDb()
    .selectFrom("organization_members")
    .innerJoin("users", "users.id", "organization_members.user_id")
    .select(["users.id as id", "users.name as name", "users.email as email"])
    .where("organization_members.organization_id", "=", organizationId)
    .where("organization_members.user_id", "in", speakerUserIds)
    .execute();
  return new Map(rows.map((row) => [row.id, { name: row.name, email: row.email }]));
}

export const transcriptRoutes = new Hono()
  .get("/v1/transcripts", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const sessionId = c.req.query("sessionId");
    const deviceId = c.req.query("deviceId");
    const limit = pageLimit(c.req.query("limit"));
    const before = cursorDate(c.req.query("before"));
    let query = getDb()
      .selectFrom("transcript_segments")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where("is_interim", "=", false)
      .orderBy("started_at", "desc")
      .limit(limit + 1);
    if (sessionId) query = query.where("session_id", "=", sessionId);
    if (deviceId) query = query.where("device_id", "=", deviceId);
    if (before) query = query.where("started_at", "<", before);
    const rows = await query.execute();
    const segments = rows.slice(0, limit);
    const speakerUsers = await speakerUsersForSegments(user.organizationId, segments);
    const nextCursor = rows.length > limit ? iso(segments[segments.length - 1]?.started_at) : null;
    return c.json({
      segments: segments.map((segment) =>
        publicSegment(
          segment,
          segment.speaker_user_id ? speakerUsers.get(segment.speaker_user_id) : null,
        ),
      ),
      nextCursor,
    });
  })
  .get("/v1/transcripts/search", async (c) => {
    const serviceContext = serviceSearchContext(c.req.header("authorization"), (name) =>
      c.req.query(name),
    );
    const user = serviceContext ?? (await requireUser(c.req.raw.headers));
    const sessionId = c.req.query("sessionId");
    const deviceId = c.req.query("deviceId");
    const queryText = searchText(c.req.query("query") ?? c.req.query("q"));
    const from = cursorDate(c.req.query("from") ?? c.req.query("start"));
    const to = cursorDate(c.req.query("to") ?? c.req.query("end"));
    const before = cursorDate(c.req.query("before"));
    const limit = pageLimit(c.req.query("limit"));
    const result = await searchTranscriptIndex({
      organizationId: user.organizationId,
      actorUserId: user.userId,
      query: queryText,
      sessionId,
      deviceId,
      from,
      to,
      before,
      limit,
    });
    const speakerUsers = await speakerUsersForIds(
      user.organizationId,
      [
        ...new Set(
          result.segments
            .map((segment) => segment.speakerUserId)
            .filter((id): id is string => Boolean(id)),
        ),
      ],
    );
    return c.json({
      ...result,
      segments: result.segments.map((segment) => {
        const speakerUser = segment.speakerUserId ? speakerUsers.get(segment.speakerUserId) : null;
        return {
          ...segment,
          speakerName: speakerUser ? accountDisplayName(speakerUser) : null,
        };
      }),
      nextCursor: null,
    });
  })
  .get("/v1/voice-sessions", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const deviceId = c.req.query("deviceId");
    const limit = pageLimit(c.req.query("limit"), 25, 50);
    const segmentLimit = pageLimit(c.req.query("segmentLimit"), 12, 50);
    const before = cursorDate(c.req.query("before"));
    let query = getDb()
      .selectFrom("voice_sessions")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where(
        sql<boolean>`voice_sessions.status = 'active' or exists (
          select 1
          from transcript_segments ts
          where ts.session_id = voice_sessions.id
            and ts.organization_id = ${user.organizationId}
            and ts.is_interim = false
        )`,
      )
      .orderBy("started_at", "desc")
      .limit(limit + 1);
    if (deviceId) query = query.where("device_id", "=", deviceId);
    if (before) query = query.where("started_at", "<", before);

    const rows = await query.execute();
    const sessions = rows.slice(0, limit);
    const sessionIds = sessions.map((session) => session.id);
    const allSegments = sessionIds.length
      ? await getDb()
          .selectFrom("transcript_segments")
          .selectAll()
          .where("organization_id", "=", user.organizationId)
          .where("is_interim", "=", false)
          .where("session_id", "in", sessionIds)
          .orderBy("started_at", "asc")
          .execute()
      : [];
    const segmentsBySession = new Map<string, TranscriptSegmentRow[]>();
    for (const segment of allSegments) {
      const current = segmentsBySession.get(segment.session_id) ?? [];
      if (current.length >= segmentLimit) continue;
      current.push(segment);
      segmentsBySession.set(segment.session_id, current);
    }
    const segments = [...segmentsBySession.values()].flat();
    const speakerUsers = await speakerUsersForSegments(user.organizationId, segments);
    const nextCursor = rows.length > limit ? iso(sessions[sessions.length - 1]?.started_at) : null;
    return c.json({
      sessions: sessions.map((session) =>
        publicVoiceSession(
          session,
          segmentsBySession.get(session.id) ?? [],
          speakerUsers,
          { includeWords: false },
        ),
      ),
      nextCursor,
    });
  })
  .post("/v1/transcripts/events", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("transcript_ingest_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const body = transcriptEventSchema.parse(await c.req.json());
    const device = body.deviceId
      ? await getDb()
          .selectFrom("devices")
          .selectAll()
          .where("id", "=", body.deviceId)
          .executeTakeFirst()
      : null;
    const organizationId = body.organizationId ?? device?.organization_id;
    if (!organizationId) {
      logWarn("transcript_ingest_rejected", { reason: "missing_organization" });
      return c.json({ error: "Missing organization" }, 400);
    }
    const userId = body.userId ?? device?.user_id;
    if (!userId) {
      logWarn("transcript_ingest_rejected", { reason: "missing_user" });
      return c.json({ error: "Missing user" }, 400);
    }
    const identifiedSpeaker = body.speakerUserId
      ? await getDb()
          .selectFrom("organization_members")
          .innerJoin("users", "users.id", "organization_members.user_id")
          .select(["users.id as id", "users.email as email", "users.name as name"])
          .where("organization_members.organization_id", "=", organizationId)
          .where("organization_members.user_id", "=", body.speakerUserId)
          .executeTakeFirst()
      : null;
    if (body.speakerUserId && !identifiedSpeaker) {
      logWarn("transcript_ingest_rejected", { reason: "speaker_not_in_organization" });
      return c.json({ error: "Speaker is not in organization" }, 400);
    }
    const words = body.words ? jsonb(body.words) : null;
    const segment = await getDb()
      .insertInto("transcript_segments")
      .values({
        id: body.segmentId ?? id("seg"),
        organization_id: organizationId,
        user_id: userId,
        device_id: body.deviceId ?? null,
        session_id: body.sessionId,
        source: body.source,
        text: body.transcript,
        words,
        is_interim: !body.isFinal,
        speaker_label: body.speakerId ?? null,
        speaker_user_id: identifiedSpeaker?.id ?? null,
        speaker_name: null,
        speaker_confidence: identifiedSpeaker ? (body.speakerConfidence ?? null) : null,
        emotion_label: body.emotionLabel ?? null,
        emotion_confidence: body.emotionConfidence ?? null,
        emotion_model: body.emotionModel ?? null,
        confidence: body.confidence ?? null,
        started_at: dateFrom(body.startedAt),
        ended_at: body.isFinal ? dateFrom(body.endedAt) : null,
        created_at: now(),
      })
      .onConflict((oc) =>
        oc.column("id").doUpdateSet({
          text: body.transcript,
          words,
          is_interim: !body.isFinal,
          speaker_label: body.speakerId ?? null,
          speaker_user_id: identifiedSpeaker?.id ?? sql`transcript_segments.speaker_user_id`,
          speaker_name: identifiedSpeaker ? null : sql`transcript_segments.speaker_name`,
          speaker_confidence: identifiedSpeaker
            ? (body.speakerConfidence ?? null)
            : sql`transcript_segments.speaker_confidence`,
          emotion_label: body.emotionLabel ?? sql`transcript_segments.emotion_label`,
          emotion_confidence: body.emotionConfidence ?? sql`transcript_segments.emotion_confidence`,
          emotion_model: body.emotionModel ?? sql`transcript_segments.emotion_model`,
          confidence: body.confidence ?? null,
          ended_at: body.isFinal ? dateFrom(body.endedAt) : null,
        }),
      )
      .returningAll()
      .executeTakeFirstOrThrow();
    if (body.isFinal && identifiedSpeaker && body.speakerId) {
      await getDb()
        .updateTable("transcript_segments")
        .set({
          speaker_user_id: identifiedSpeaker.id,
          speaker_name: null,
          speaker_confidence: body.speakerConfidence ?? null,
        })
        .where("organization_id", "=", organizationId)
        .where("session_id", "=", body.sessionId)
        .where("speaker_label", "=", body.speakerId)
        .where("speaker_user_id", "is", null)
        .execute();
    }
    const segmentSpeakerUser =
      identifiedSpeaker ??
      (segment.speaker_user_id
        ? (await speakerUsersForIds(organizationId, [segment.speaker_user_id])).get(segment.speaker_user_id) ?? null
        : null);
    publishOrganization(organizationId, {
      type: body.isFinal ? "transcript.segment.created" : "transcript.interim.updated",
      source: "hardware",
      data: { deviceId: body.deviceId ?? undefined, segment: publicSegment(segment, segmentSpeakerUser) },
    });
    if (body.isFinal) void indexTranscriptSegment(segment);
    logInfo("transcript_ingested", {
      userId: redactId(userId),
      organizationId: redactId(organizationId),
      deviceId: redactId(body.deviceId ?? null),
      sessionId: redactId(body.sessionId),
      segmentId: redactId(segment.id),
      isFinal: body.isFinal,
      textLength: body.transcript.length,
    });
    return c.json({ segment: publicSegment(segment, segmentSpeakerUser) });
  });
