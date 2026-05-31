import { createHmac, timingSafeEqual } from "node:crypto";
import { type Context, Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { z } from "zod";
import { loadConfig } from "../config.js";
import { getDb } from "../db/client.js";
import {
  createAgentRun,
  publicAgentRun,
  waitForAgentRun,
  type AgentAction,
  type CodexThreadMode,
} from "../lib/agent-runs.js";
import {
  listAgentCompletions,
  markAgentCompletionDelivered,
  publicAgentCompletion,
} from "../lib/agent-events.js";
import { requireDevice, requireUser } from "../lib/auth.js";
import { publishOrganization } from "../lib/events.js";
import { id, now } from "../lib/ids.js";
import { errorMessage, logInfo, logWarn, redactId } from "../lib/log.js";
import { decryptSecret } from "../lib/secrets.js";
import { publicDevice } from "../lib/shape.js";
import { accountDisplayName } from "../lib/speaker-profiles.js";
import {
  deleteUserMemory,
  listUserMemories,
  publicUserMemory,
  saveUserMemory,
  updateUserMemory,
  type UserMemoryConfidence,
  type UserMemoryKind,
} from "../lib/user-memories.js";
import {
  soundRecognitionSettings,
  listeningEnabled,
  voiceLlmSettings,
  voiceVocabulary,
} from "../lib/voice-settings.js";
import {
  currentSpeakerVolume,
  nextSpeakerVolume,
  syncSpeakerVolume,
  volumeActionSchema,
} from "../lib/voice-volume.js";

const voiceSessionSchema = z.object({
  sampleRate: z.number().int().min(8000).max(48000).default(16000),
  channels: z.number().int().min(1).max(8).default(1),
  initialAwake: z.boolean().default(false),
});

const endVoiceSessionSchema = z.object({
  reason: z.string().trim().max(80).optional(),
});

const userMemorySchema = z.object({
  kind: z.enum(["fact", "preference", "instruction"]).default("fact"),
  content: z.string().trim().min(1).max(500),
  confidence: z.enum(["explicit", "high", "medium"]).default("high"),
});

const userMemoryUpdateSchema = z.object({
  kind: z.enum(["fact", "preference", "instruction"]).optional(),
  content: z.string().trim().min(1).max(500).optional(),
  confidence: z.enum(["explicit", "high", "medium"]).optional(),
});

const codexReasoningEffortSchema = z.preprocess(
  (value) => typeof value === "string" ? value.trim().toLowerCase() : value,
  z.enum(["none", "minimal", "low", "medium", "high", "xhigh"]),
);

const codexReasoningSummarySchema = z.preprocess(
  (value) => typeof value === "string" ? value.trim().toLowerCase() : value,
  z.enum(["auto", "concise", "detailed", "none"]),
);

const codexThinkingSchema = z.object({
  effort: codexReasoningEffortSchema.optional(),
  summary: codexReasoningSummarySchema.optional(),
});

const agentActionSchema = z.object({
  agentId: z.string().trim().min(1).optional(),
  threadId: z.string().trim().min(1).optional(),
  thread: z.enum(["auto", "same", "new"]).default("auto"),
  action: z.enum(["start", "steer", "interrupt", "status"]).optional(),
  prompt: z.string().trim().min(1).max(8000).optional(),
  context: z.string().trim().max(8000).optional(),
  responseStyle: z.enum(["brief", "normal", "detailed"]).optional(),
  delivery: z.enum(["auto", "speak", "save", "silent"]).default("auto"),
  thinking: codexThinkingSchema.optional(),
  reasoning: codexThinkingSchema.optional(),
  reasoningEffort: codexReasoningEffortSchema.optional(),
  reasoningSummary: codexReasoningSummarySchema.optional(),
  waitMs: z.number().int().min(0).max(35000).default(0),
});

function codexThinkingFromBody(
  body: z.infer<typeof agentActionSchema>,
  options: { defaultEffort?: z.infer<typeof codexReasoningEffortSchema> } = {},
) {
  const thinking = body.thinking ?? body.reasoning ?? {};
  const effort = thinking.effort ?? body.reasoningEffort ?? options.defaultEffort;
  const summary = thinking.summary ?? body.reasoningSummary;
  return effort || summary
    ? {
        ...(effort ? { effort } : {}),
        ...(summary ? { summary } : {}),
      }
    : undefined;
}

const lightActionSchema = z.object({
  effect: z.enum(["off", "breath", "rainbow", "solid", "doa"]).optional(),
  color: z
    .string()
    .trim()
    .regex(/^(#|0x)?[0-9a-fA-F]{6}$/)
    .optional(),
  brightness: z.number().min(0).max(255).optional(),
});

async function decryptOptionalVoiceSecret(
  value: string | null | undefined,
  context: Parameters<typeof decryptSecret>[1],
  fields: Record<string, unknown>,
) {
  try {
    return await decryptSecret(value, context);
  } catch (error) {
    logWarn("voice_session_secret_decrypt_failed", {
      ...fields,
      error: errorMessage(error),
    });
    return null;
  }
}

async function handleVoiceAgentAction(c: Context) {
  const serviceApiKey = loadConfig().serviceApiKey;
  if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
    logWarn("voice_session_agent_rejected", { reason: "unauthorized" });
    throw new HTTPException(401, { message: "Unauthorized" });
  }
  const body = agentActionSchema.parse(await c.req.json().catch(() => ({})));
  const action = (body.action ?? (body.prompt ? "start" : "status")) as AgentAction;
  if ((action === "start" || action === "steer") && !body.prompt) {
    throw new HTTPException(400, { message: `${action} requires prompt` });
  }
  const sessionId = c.req.param("id");
  if (!sessionId) throw new HTTPException(400, { message: "Voice session id is required" });
  const session = await getDb()
    .selectFrom("voice_sessions")
    .selectAll()
    .where("id", "=", sessionId)
    .executeTakeFirst();
  if (!session) throw new HTTPException(404, { message: "Voice session not found" });
  const run = await createAgentRun({
    organizationId: session.organization_id,
    userId: session.user_id,
    sessionId: session.id,
    sourceDeviceId: session.device_id,
    agentId: body.agentId,
    threadId: body.threadId,
    thread: body.thread as CodexThreadMode,
    action,
    prompt: body.prompt,
    context: body.context,
    responseStyle: body.responseStyle,
    delivery: body.delivery,
    thinking: codexThinkingFromBody(body, {
      defaultEffort: action === "start" || action === "steer" ? "low" : undefined,
    }),
  });
  publishOrganization(session.organization_id, {
    type: "agent.run.created",
    source: "voice",
    data: { run: publicAgentRun(run) },
  });
  logInfo("voice_session_agent_run_created", {
    sessionId: redactId(session.id),
    deviceId: redactId(session.device_id),
    runId: redactId(run.id),
    agentId: redactId(run.agent_id),
    action,
  });
  const finalRun = body.waitMs > 0 ? await waitForAgentRun(run.id, body.waitMs) : run;
  return c.json({
    ok: finalRun.status === "completed",
    requestId: finalRun.id,
    agentId: finalRun.agent_id,
    status: finalRun.status,
    agent: finalRun.result,
    error: finalRun.error,
    run: publicAgentRun(finalRun),
  });
}

async function handleVoiceAgentCompletions(c: Context) {
  const serviceApiKey = loadConfig().serviceApiKey;
  if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
    logWarn("voice_session_agent_completions_rejected", { reason: "unauthorized" });
    throw new HTTPException(401, { message: "Unauthorized" });
  }
  const sessionId = c.req.param("id");
  if (!sessionId) throw new HTTPException(400, { message: "Voice session id is required" });
  const session = await getDb()
    .selectFrom("voice_sessions")
    .select(["id", "organization_id"])
    .where("id", "=", sessionId)
    .executeTakeFirst();
  if (!session) throw new HTTPException(404, { message: "Voice session not found" });
  const completions = await listAgentCompletions(session.organization_id, {
    sessionId: session.id,
    after: c.req.query("after"),
    limit: Number(c.req.query("limit") || 20),
    undeliveredOnly: true,
  });
  return c.json({ completions: completions.map(publicAgentCompletion) });
}

async function handleVoiceAgentCompletionDelivered(c: Context) {
  const serviceApiKey = loadConfig().serviceApiKey;
  if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
    logWarn("voice_session_agent_completion_delivered_rejected", { reason: "unauthorized" });
    throw new HTTPException(401, { message: "Unauthorized" });
  }
  const sessionId = c.req.param("id");
  const completionId = c.req.param("completionId");
  if (!sessionId) throw new HTTPException(400, { message: "Voice session id is required" });
  if (!completionId) throw new HTTPException(400, { message: "Completion id is required" });
  const session = await getDb()
    .selectFrom("voice_sessions")
    .select(["id", "organization_id"])
    .where("id", "=", sessionId)
    .executeTakeFirst();
  if (!session) throw new HTTPException(404, { message: "Voice session not found" });
  const completion = await markAgentCompletionDelivered({
    completionId,
    sessionId: session.id,
    organizationId: session.organization_id,
  });
  return c.json({ completion: publicAgentCompletion(completion) });
}

async function requireServiceVoiceSession(c: Context) {
  const serviceApiKey = loadConfig().serviceApiKey;
  if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
    logWarn("voice_session_memory_rejected", { reason: "unauthorized" });
    throw new HTTPException(401, { message: "Unauthorized" });
  }
  const sessionId = c.req.param("id");
  if (!sessionId) throw new HTTPException(400, { message: "Voice session id is required" });
  const session = await getDb()
    .selectFrom("voice_sessions")
    .select(["id", "organization_id", "user_id", "device_id"])
    .where("id", "=", sessionId)
    .executeTakeFirst();
  if (!session) throw new HTTPException(404, { message: "Voice session not found" });
  return session;
}

async function handleVoiceMemoryList(c: Context) {
  const session = await requireServiceVoiceSession(c);
  const memories = await listUserMemories({
    organizationId: session.organization_id,
    userId: session.user_id,
    limit: Number(c.req.query("limit") || 24),
  });
  return c.json({ memories: memories.map(publicUserMemory) });
}

async function handleVoiceMemoryCreate(c: Context) {
  const session = await requireServiceVoiceSession(c);
  const body = userMemorySchema.parse(await c.req.json().catch(() => ({})));
  const memory = await saveUserMemory({
    organizationId: session.organization_id,
    userId: session.user_id,
    sourceDeviceId: session.device_id,
    sourceSessionId: session.id,
    kind: body.kind as UserMemoryKind,
    content: body.content,
    confidence: body.confidence as UserMemoryConfidence,
    metadata: { source: "voice" },
  });
  const publicMemory = publicUserMemory(memory);
  publishOrganization(session.organization_id, {
    type: "user.memory.saved",
    source: "voice",
    data: { memory: publicMemory },
  });
  logInfo("voice_session_memory_saved", {
    sessionId: redactId(session.id),
    deviceId: redactId(session.device_id),
    memoryId: redactId(memory.id),
    kind: memory.kind,
    confidence: memory.confidence,
    chars: memory.content.length,
  });
  return c.json({ ok: true, memory: publicMemory });
}

async function handleVoiceMemoryUpdate(c: Context) {
  const session = await requireServiceVoiceSession(c);
  const memoryId = c.req.param("memoryId");
  if (!memoryId) throw new HTTPException(400, { message: "Memory id is required" });
  const body = userMemoryUpdateSchema.parse(await c.req.json().catch(() => ({})));
  const memory = await updateUserMemory({
    organizationId: session.organization_id,
    userId: session.user_id,
    memoryId,
    kind: body.kind as UserMemoryKind | undefined,
    content: body.content,
    confidence: body.confidence as UserMemoryConfidence | undefined,
    metadata: { source: "voice" },
  });
  if (!memory) throw new HTTPException(404, { message: "Memory not found" });
  const publicMemory = publicUserMemory(memory);
  publishOrganization(session.organization_id, {
    type: "user.memory.updated",
    source: "voice",
    data: { memory: publicMemory },
  });
  logInfo("voice_session_memory_updated", {
    sessionId: redactId(session.id),
    deviceId: redactId(session.device_id),
    memoryId: redactId(memory.id),
    kind: memory.kind,
    confidence: memory.confidence,
    chars: memory.content.length,
  });
  return c.json({ ok: true, memory: publicMemory });
}

async function handleVoiceMemoryDelete(c: Context) {
  const session = await requireServiceVoiceSession(c);
  const memoryId = c.req.param("memoryId");
  if (!memoryId) throw new HTTPException(400, { message: "Memory id is required" });
  const memory = await deleteUserMemory({
    organizationId: session.organization_id,
    userId: session.user_id,
    memoryId,
  });
  if (!memory) throw new HTTPException(404, { message: "Memory not found" });
  const publicMemory = publicUserMemory(memory);
  publishOrganization(session.organization_id, {
    type: "user.memory.deleted",
    source: "voice",
    data: { memory: publicMemory },
  });
  logInfo("voice_session_memory_deleted", {
    sessionId: redactId(session.id),
    deviceId: redactId(session.device_id),
    memoryId: redactId(memory.id),
  });
  return c.json({ ok: true, memory: publicMemory });
}

function base64UrlJson(value: unknown) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function sign(payload: string) {
  return createHmac("sha256", loadConfig().tokenSecret).update(payload).digest("base64url");
}

function signedToken(payload: unknown) {
  const encoded = base64UrlJson(payload);
  return `${encoded}.${sign(encoded)}`;
}

export function verifySignedVoiceToken(token: string) {
  const [encoded, signature] = token.split(".");
  if (!encoded || !signature) return null;
  const expected = sign(encoded);
  const left = Buffer.from(signature);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !timingSafeEqual(left, right)) return null;
  const payload = JSON.parse(Buffer.from(encoded, "base64url").toString("utf-8")) as {
    exp?: number;
    sessionId?: string;
    deviceId?: string;
    userId?: string;
    organizationId?: string;
    sampleRate?: number;
    channels?: number;
  };
  if (!payload.exp || payload.exp < Math.floor(Date.now() / 1000)) return null;
  return payload;
}

export const voiceRoutes = new Hono()
  .get("/v1/voice/vocabulary", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const keyterms = await voiceVocabulary(user.organizationId);
    logInfo("voice_vocabulary_loaded", {
      userId: redactId(user.userId),
      sttKeytermCount: keyterms.length,
    });
    return c.json({ keyterms });
  })
  .post("/v1/voice/sessions", async (c) => {
    const config = loadConfig();
    if (!config.voice.url) {
      throw new HTTPException(503, { message: "Voice URL is not configured" });
    }
    const deviceContext = await requireDevice(c.req.raw.headers);
    const body = voiceSessionSchema.parse(await c.req.json().catch(() => ({})));
    const device = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("id", "=", deviceContext.deviceId)
      .executeTakeFirstOrThrow();
    const issuedAt = Math.floor(Date.now() / 1000);
    const startedAt = now();
    const sessionId = id("voice");
    const payload = {
      sessionId,
      deviceId: device.id,
      userId: device.user_id,
      organizationId: device.organization_id,
      source: "device",
      sampleRate: body.sampleRate,
      channels: body.channels,
      initialAwake: body.initialAwake,
      iat: issuedAt,
      exp: issuedAt + 10 * 60,
    };
    const token = signedToken(payload);
    const voiceUrl = new URL(config.voice.url);
    voiceUrl.protocol = voiceUrl.protocol === "https:" || voiceUrl.protocol === "wss:" ? "wss:" : "ws:";
    if (voiceUrl.pathname === "/" || voiceUrl.pathname === "") voiceUrl.pathname = "/ws";
    voiceUrl.searchParams.set("token", token);
    const updatedDevice = await getDb().transaction().execute(async (trx) => {
      await trx
        .updateTable("voice_sessions")
        .set({
          status: "ended",
          ended_at: startedAt,
          updated_at: startedAt,
        })
        .where("device_id", "=", device.id)
        .where("status", "=", "active")
        .where("ended_at", "is", null)
        .execute();
      await trx
        .insertInto("voice_sessions")
        .values({
          id: sessionId,
          organization_id: device.organization_id,
          user_id: device.user_id,
          device_id: device.id,
          source: "device",
          room_name: sessionId,
          status: "active",
          started_at: startedAt,
          ended_at: null,
          created_at: startedAt,
          updated_at: startedAt,
        })
        .execute();
      return trx
        .updateTable("devices")
        .set({
          status: "listening",
          last_seen_at: startedAt,
          updated_at: startedAt,
        })
        .where("id", "=", device.id)
        .returningAll()
        .executeTakeFirst();
    });
    if (updatedDevice) {
      publishOrganization(updatedDevice.organization_id, {
        type: "device.updated",
        source: "hardware",
        data: { device: publicDevice(updatedDevice) },
      });
      publishOrganization(updatedDevice.organization_id, {
        type: "session.started",
        source: "hardware",
        data: { deviceId: updatedDevice.id, session: { id: sessionId, roomName: sessionId } },
      });
    }
    logInfo("voice_session_created", {
      sessionId: redactId(sessionId),
      deviceId: redactId(device.id),
      userId: redactId(device.user_id),
      sampleRate: body.sampleRate,
      channels: body.channels,
      voiceProtocol: voiceUrl.protocol.replace(":", ""),
      voiceHost: voiceUrl.host,
      voicePath: voiceUrl.pathname,
    });
    return c.json({
      voiceUrl: voiceUrl.toString(),
      sessionId,
      expiresAt: new Date(payload.exp * 1000).toISOString(),
    });
  })
  .get("/v1/voice/sessions/:id/vocabulary", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_vocabulary_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const session = await getDb()
      .selectFrom("voice_sessions")
      .select(["organization_id"])
      .where("id", "=", c.req.param("id"))
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });
    const keyterms = await voiceVocabulary(session.organization_id);
    logInfo("voice_session_vocabulary_loaded", {
      sessionId: redactId(c.req.param("id")),
      sttKeytermCount: keyterms.length,
    });
    return c.json({ keyterms });
  })
  .get("/v1/voice/sessions/:id/config", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_config_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const session = await getDb()
      .selectFrom("voice_sessions")
      .innerJoin("devices", "devices.id", "voice_sessions.device_id")
      .leftJoin("device_secrets", "device_secrets.device_id", "devices.id")
      .select([
        "devices.id as device_id",
        "voice_sessions.user_id as user_id",
        "voice_sessions.organization_id as organization_id",
        "devices.settings as settings",
        "device_secrets.llm_api_key_ciphertext as llm_api_key_ciphertext",
      ])
      .where("voice_sessions.id", "=", c.req.param("id"))
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });
    const keyterms = await voiceVocabulary(session.organization_id);
    const speakerProfiles = await getDb()
      .selectFrom("speaker_profiles")
      .innerJoin("users", "users.id", "speaker_profiles.user_id")
      .select([
        "speaker_profiles.id as id",
        "speaker_profiles.user_id as user_id",
        "speaker_profiles.provider as provider",
        "speaker_profiles.model as model",
        "speaker_profiles.embedding_ciphertext as embedding_ciphertext",
        "users.name as name",
        "users.email as email",
      ])
      .where("speaker_profiles.organization_id", "=", session.organization_id)
      .where("speaker_profiles.status", "=", "registered")
      .where("speaker_profiles.embedding_ciphertext", "is not", null)
      .execute();
    const llm = {
      ...voiceLlmSettings(session.settings),
      apiKey: await decryptOptionalVoiceSecret(
        session.llm_api_key_ciphertext,
        { deviceId: session.device_id },
        {
          sessionId: redactId(c.req.param("id")),
          deviceId: redactId(session.device_id),
          secret: "llm_api_key",
        },
      ),
    };
    const speakers = await Promise.all(
      speakerProfiles.map(async (profile) => {
        const embedding = await decryptOptionalVoiceSecret(profile.embedding_ciphertext, {
          organizationId: session.organization_id,
          userId: profile.user_id,
          speakerProfileId: profile.id,
        }, {
          sessionId: redactId(c.req.param("id")),
          speakerProfileId: redactId(profile.id),
          secret: "speaker_embedding",
        });
        return embedding
          ? {
              id: profile.id,
              userId: profile.user_id,
              displayName: accountDisplayName(profile),
              provider: profile.provider,
              model: profile.model,
              embedding,
            }
          : null;
      }),
    ).then((profiles) => profiles.filter((profile) => profile !== null));
    const memories = await listUserMemories({
      organizationId: session.organization_id,
      userId: session.user_id,
      limit: 24,
    });
    logInfo("voice_session_config_loaded", {
      sessionId: redactId(c.req.param("id")),
      sttKeytermCount: keyterms.length,
      speakerProfileCount: speakers.length,
      memoryCount: memories.length,
      llmOverride: Boolean(llm.baseUrl || llm.model),
      llmApiKeyConfigured: Boolean(llm.apiKey),
    });
    return c.json({
      keyterms,
      llm,
      soundRecognition: soundRecognitionSettings(session.settings),
      speakerRecognition: {
        provider: "speechbrain-ecapa",
        profiles: speakers,
      },
      memories: memories.map(publicUserMemory),
    });
  })
  .post("/v1/voice/sessions/:id/events/token", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_events_token_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const session = await getDb()
      .selectFrom("voice_sessions")
      .select(["id", "user_id"])
      .where("id", "=", c.req.param("id"))
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });
    logInfo("voice_session_events_token_created", {
      sessionId: redactId(session.id),
    });
    throw new HTTPException(410, { message: "Hosted realtime events have been removed" });
  })
  .post("/v1/voice/sessions/:id/device-volume", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_volume_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const body = volumeActionSchema.parse(await c.req.json().catch(() => ({})));
    const sessionId = c.req.param("id");
    const session = await getDb()
      .selectFrom("voice_sessions")
      .innerJoin("devices", "devices.id", "voice_sessions.device_id")
      .select([
        "voice_sessions.user_id as user_id",
        "voice_sessions.organization_id as organization_id",
        "voice_sessions.device_id as device_id",
        "devices.settings as settings",
        "devices.hardware_info as hardware_info",
      ])
      .where("voice_sessions.id", "=", sessionId)
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });

    const currentVolume = currentSpeakerVolume(session.settings, session.hardware_info);
    const speakerVolume = nextSpeakerVolume(currentVolume, body.action, body.volume);
    const settings =
      session.settings && typeof session.settings === "object"
        ? { ...(session.settings as Record<string, unknown>), speakerVolume }
        : { speakerVolume };
    const device = await getDb()
      .updateTable("devices")
      .set({
        settings,
        updated_at: now(),
      })
      .where("id", "=", session.device_id)
      .returningAll()
      .executeTakeFirstOrThrow();
    const deviceSyncOk = body.syncDevice
      ? await syncSpeakerVolume(session.device_id, speakerVolume)
      : false;
    publishOrganization(session.organization_id, {
      type: "device.updated",
      source: "hardware",
      data: { device: publicDevice(device) },
    });
    logInfo("voice_session_volume_changed", {
      sessionId: redactId(sessionId),
      deviceId: redactId(session.device_id),
      action: body.action,
      currentVolume,
      speakerVolume,
      syncDevice: body.syncDevice,
      deviceSyncOk,
    });
    return c.json({ deviceId: session.device_id, volume: speakerVolume, deviceSyncOk });
  })
  .post("/v1/voice/sessions/:id/device-light", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_light_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const body = lightActionSchema.parse(await c.req.json().catch(() => ({})));
    if (body.effect === undefined && body.color === undefined && body.brightness === undefined) {
      throw new HTTPException(400, { message: "No light setting provided" });
    }
    const sessionId = c.req.param("id");
    const session = await getDb()
      .selectFrom("voice_sessions")
      .innerJoin("devices", "devices.id", "voice_sessions.device_id")
      .select([
        "voice_sessions.user_id as user_id",
        "voice_sessions.organization_id as organization_id",
        "voice_sessions.device_id as device_id",
        "devices.settings as settings",
      ])
      .where("voice_sessions.id", "=", sessionId)
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });

    const settings =
      session.settings && typeof session.settings === "object"
        ? { ...(session.settings as Record<string, unknown>), statusLight: body }
        : { statusLight: body };
    const device = await getDb()
      .updateTable("devices")
      .set({
        settings,
        updated_at: now(),
      })
      .where("id", "=", session.device_id)
      .returningAll()
      .executeTakeFirstOrThrow();
    publishOrganization(session.organization_id, {
      type: "device.updated",
      source: "hardware",
      data: { device: publicDevice(device) },
    });
    logInfo("voice_session_light_changed", {
      sessionId: redactId(sessionId),
      deviceId: redactId(session.device_id),
      effect: body.effect,
      color: body.color,
      brightness: body.brightness,
    });
    return c.json({ deviceId: session.device_id, statusLight: body });
  })
  .post("/v1/voice/sessions/:id/agent", handleVoiceAgentAction)
  .get("/v1/voice/sessions/:id/agent/completions", handleVoiceAgentCompletions)
  .post("/v1/voice/sessions/:id/agent/completions/:completionId/delivered", handleVoiceAgentCompletionDelivered)
  .get("/v1/voice/sessions/:id/memories", handleVoiceMemoryList)
  .post("/v1/voice/sessions/:id/memories", handleVoiceMemoryCreate)
  .patch("/v1/voice/sessions/:id/memories/:memoryId", handleVoiceMemoryUpdate)
  .delete("/v1/voice/sessions/:id/memories/:memoryId", handleVoiceMemoryDelete)
  .post("/v1/voice/sessions/:id/end", async (c) => {
    const serviceApiKey = loadConfig().serviceApiKey;
    if (serviceApiKey && c.req.header("authorization") !== `Bearer ${serviceApiKey}`) {
      logWarn("voice_session_end_rejected", { reason: "unauthorized" });
      throw new HTTPException(401, { message: "Unauthorized" });
    }
    const body = endVoiceSessionSchema.parse(await c.req.json().catch(() => ({})));
    const endedAt = now();
    const sessionId = c.req.param("id");
    const session = await getDb()
      .selectFrom("voice_sessions")
      .innerJoin("devices", "devices.id", "voice_sessions.device_id")
      .select([
        "voice_sessions.id as id",
        "voice_sessions.user_id as user_id",
        "voice_sessions.organization_id as organization_id",
        "voice_sessions.device_id as device_id",
        "devices.settings as settings",
        "devices.status as device_status",
      ])
      .where("voice_sessions.id", "=", sessionId)
      .executeTakeFirst();
    if (!session) throw new HTTPException(404, { message: "Voice session not found" });
    const nextDeviceStatus = listeningEnabled(session.settings) ? "online" : "muted";
    const updatedDevice = await getDb().transaction().execute(async (trx) => {
      await trx
        .updateTable("voice_sessions")
        .set({
          status: "ended",
          ended_at: endedAt,
          updated_at: endedAt,
        })
        .where("id", "=", sessionId)
        .where("ended_at", "is", null)
        .execute();
      if (session.device_status !== "listening") return null;
      return trx
        .updateTable("devices")
        .set({
          status: nextDeviceStatus,
          last_seen_at: endedAt,
          updated_at: endedAt,
        })
        .where("id", "=", session.device_id)
        .returningAll()
        .executeTakeFirst();
    });
    publishOrganization(session.organization_id, {
      type: "session.ended",
      source: "hardware",
      data: { deviceId: session.device_id, sessionId: session.id, endedAt: endedAt.toISOString() },
    });
    if (updatedDevice) {
      publishOrganization(updatedDevice.organization_id, {
        type: "device.updated",
        source: "hardware",
        data: { device: publicDevice(updatedDevice) },
      });
    }
    logInfo("voice_session_ended", {
      sessionId: redactId(sessionId),
      reason: body.reason ?? "unknown",
    });
    return c.json({ ok: true });
  });
