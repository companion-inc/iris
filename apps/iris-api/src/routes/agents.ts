import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { randomInt } from "node:crypto";
import { z } from "zod";
import { getDb } from "../db/client.js";
import { type DeviceRow } from "../db/types.js";
import { requireDevice, requireUser } from "../lib/auth.js";
import {
  claimNextAgentRun,
  completeAgentRun,
  createAgentRun,
  listAgentRuns,
  listCodexThreads,
  publicAgentRun,
  publicAgentRunEvent,
  publicCodexThread,
  syncCodexThreadsFromHeartbeat,
  TERMINAL_RUN_STATUSES,
  waitForAgentRun,
  type AgentAction,
  type CodexThreadMode,
} from "../lib/agent-runs.js";
import {
  createAgentApproval,
  createAgentCompletionForRun,
  getAgentApprovalForDevice,
  listAgentApprovals,
  listAgentCompletions,
  publicAgentApproval,
  publicAgentCompletion,
  publicAgentCompletionEvent,
  resolveAgentApproval,
} from "../lib/agent-events.js";
import { publishOrganization } from "../lib/events.js";
import { id, now } from "../lib/ids.js";
import { publicDevice } from "../lib/shape.js";
import { opaqueToken, tokenHash } from "../lib/tokens.js";

const AGENT_KIND = "agent";
const AGENT_ONLINE_TTL_MS = 45_000;
const OFFER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const OFFER_TTL_MS = 10 * 60 * 1000;
const OFFER_PENDING_TTL_MS = 5 * 60 * 1000;

type AgentSettings = {
  role: "agent";
  agentId: string | null;
  available: boolean;
  codexEnabled: boolean;
};

const pairSchema = z.object({
  name: z.string().trim().min(1).max(80).optional(),
});

const updateAgentSchema = z.object({
  name: z.string().trim().min(1).max(80).optional(),
});

const localAgentSchema = z.object({
  agentId: z.string().trim().min(1).max(80).optional(),
  name: z.string().trim().min(1).max(80).optional(),
  hostname: z.string().trim().max(120).optional(),
  platform: z.string().trim().max(80).optional(),
  arch: z.string().trim().max(80).optional(),
  codexVersion: z.string().trim().max(120).optional(),
  bridgeUrl: z.string().trim().max(500).optional(),
});

const claimSchema = z.object({
  token: z.string().trim().min(1),
  hostname: z.string().trim().max(120).optional(),
  platform: z.string().trim().max(80).optional(),
  arch: z.string().trim().max(80).optional(),
  codexVersion: z.string().trim().max(120).optional(),
  bridgeUrl: z.string().trim().max(500).optional(),
});

const heartbeatSchema = z.object({
  hostname: z.string().trim().max(120).optional(),
  platform: z.string().trim().max(80).optional(),
  arch: z.string().trim().max(80).optional(),
  codexVersion: z.string().trim().max(120).optional(),
  bridgeUrl: z.string().trim().max(500).optional(),
  active: z.boolean().optional(),
  activeRun: z.unknown().optional(),
  threads: z.array(z.unknown()).optional(),
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

const agentRunSchema = z.object({
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
  body: z.infer<typeof agentRunSchema>,
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

const agentRunCompleteSchema = z.object({
  status: z.enum(["running", "completed", "failed", "cancelled", "interrupted"]).default("completed"),
  result: z.unknown().optional(),
  error: z.string().trim().max(2000).nullable().optional(),
});

const agentApprovalCreateSchema = z.object({
  codexRequestId: z.string().trim().max(200).optional(),
  codexMethod: z.string().trim().min(1).max(200),
  request: z.unknown(),
  expiresMs: z.number().int().min(1000).max(10 * 60 * 1000).default(5 * 60 * 1000),
});

const agentApprovalResolveSchema = z.object({
  status: z.enum(["approved", "declined", "cancelled"]),
  response: z.unknown().optional(),
  error: z.string().trim().max(2000).nullable().optional(),
});

const offerSchema = z.object({
  token: z.string().trim().min(1).optional(),
  name: z.string().trim().min(1).max(80).optional(),
  hostname: z.string().trim().max(120).optional(),
  platform: z.string().trim().max(80).optional(),
  arch: z.string().trim().max(80).optional(),
  codexVersion: z.string().trim().max(120).optional(),
  bridgeUrl: z.string().trim().max(500).optional(),
});

const selectOfferSchema = z
  .object({
    offerId: z.string().trim().min(1).optional(),
    code: z.string().trim().min(1).optional(),
  })
  .refine((value) => Boolean(value.offerId || value.code), {
    message: "offerId or code is required",
  });

function agentSettings(): AgentSettings {
  return {
    role: "agent",
    agentId: null,
    available: false,
    codexEnabled: true,
  };
}

function readAgentSettings(settings: unknown): AgentSettings {
  const record = settings && typeof settings === "object" ? (settings as Record<string, unknown>) : {};
  return {
    role: "agent",
    agentId: null,
    available: typeof record.available === "boolean" ? record.available : false,
    codexEnabled: typeof record.codexEnabled === "boolean" ? record.codexEnabled : true,
  };
}

function isFreshAgentDevice(device: DeviceRow) {
  if (device.status !== "online") return false;
  const lastSeenAt = device.last_seen_at ? new Date(device.last_seen_at).getTime() : 0;
  return Number.isFinite(lastSeenAt) && Date.now() - lastSeenAt <= AGENT_ONLINE_TTL_MS;
}

function publicAgentDevice(device: DeviceRow) {
  const settings = readAgentSettings(device.settings);
  const fresh = isFreshAgentDevice(device);
  return {
    ...publicDevice(device),
    status: fresh ? device.status : "offline",
    settings: { ...settings, available: fresh && settings.available },
  };
}

function publicAgent(agent: DeviceRow) {
  return {
    ...publicAgentDevice(agent),
  };
}

function publicAgentWithThreads(agent: DeviceRow, threads: Awaited<ReturnType<typeof listCodexThreads>>) {
  return {
    ...publicAgent(agent),
    threads: threads.filter((thread) => thread.agent_id === agent.id).map(publicCodexThread),
  };
}

function publicAgentInventory(devices: DeviceRow[], threads: Awaited<ReturnType<typeof listCodexThreads>> = []) {
  const agentDevices = devices.filter((device) => device.kind === AGENT_KIND && device.status !== "pairing");
  return {
    agentDevices: agentDevices.map(publicAgentDevice),
    agents: agentDevices.map((agent) => publicAgentWithThreads(agent, threads)),
    threads: threads.map(publicCodexThread),
  };
}

function agentHardwareInfo(
  body: z.infer<typeof claimSchema> | z.infer<typeof heartbeatSchema> | z.infer<typeof localAgentSchema>,
) {
  return {
    hostname: body.hostname ?? null,
    platform: body.platform ?? null,
    arch: body.arch ?? null,
    codexVersion: body.codexVersion ?? null,
    bridgeUrl: body.bridgeUrl ?? null,
    active: "active" in body && typeof body.active === "boolean" ? body.active : null,
    activeRun: "activeRun" in body ? body.activeRun ?? null : null,
    threads: "threads" in body && Array.isArray(body.threads) ? body.threads : [],
  };
}

function activeRunIdFromHardwareInfo(value: unknown) {
  const info = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  if (info.active !== true) return null;
  const activeRun = info.activeRun && typeof info.activeRun === "object" ? (info.activeRun as Record<string, unknown>) : {};
  const id = activeRun.agentRunId ?? activeRun.runId ?? activeRun.id;
  return typeof id === "string" && id.trim() ? id.trim() : null;
}

async function upsertLocalAgent(
  user: Awaited<ReturnType<typeof requireUser>>,
  body: z.infer<typeof localAgentSchema>,
) {
  const db = getDb();
  const date = now();
  const agentId = body.agentId || id("agent");
  const name = body.name || "Iris Desktop";
  const current = await db
    .selectFrom("devices")
    .selectAll()
    .where("id", "=", agentId)
    .where("kind", "=", AGENT_KIND)
    .executeTakeFirst();
  if (current) {
    const currentSettings = readAgentSettings(current.settings);
    return db
      .updateTable("devices")
      .set({
        organization_id: user.organizationId,
        user_id: user.userId,
        name,
        status: "online",
        model: "agent",
        settings: { ...currentSettings, role: "agent", available: true },
        hardware_info: agentHardwareInfo(body),
        last_seen_at: date,
        updated_at: date,
      })
      .where("id", "=", current.id)
      .returningAll()
      .executeTakeFirstOrThrow();
  }
  return db
    .insertInto("devices")
    .values({
      id: agentId,
      organization_id: user.organizationId,
      user_id: user.userId,
      kind: AGENT_KIND,
      product: "iris-mac",
      model: "agent",
      name,
      status: "online",
      settings: agentSettings(),
      device_serial: null,
      firmware_version: null,
      hardware_info: agentHardwareInfo(body),
      last_seen_at: date,
      created_at: date,
      updated_at: date,
    })
    .returningAll()
    .executeTakeFirstOrThrow();
}

function offerMetadata(body: z.infer<typeof offerSchema>) {
  return {
    hostname: body.hostname ?? null,
    platform: body.platform ?? null,
    arch: body.arch ?? null,
    codex_version: body.codexVersion ?? null,
    bridge_url: body.bridgeUrl ?? null,
  };
}

function publicOffer(offer: {
  id: string;
  code: string;
  name: string;
  hostname: string | null;
  platform: string | null;
  arch: string | null;
  codex_version: string | null;
  bridge_url: string | null;
  status: string;
  pending_user_name: string | null;
  pending_user_email: string | null;
  device_id: string | null;
  expires_at: Date | string;
  last_seen_at: Date | string | null;
  created_at: Date | string;
  updated_at: Date | string;
}) {
  return {
    id: offer.id,
    code: offer.code,
    name: offer.name,
    hostname: offer.hostname,
    platform: offer.platform,
    arch: offer.arch,
    codexVersion: offer.codex_version,
    bridgeUrl: offer.bridge_url,
    status: offer.status,
    pendingUserName: offer.pending_user_name,
    pendingUserEmail: offer.pending_user_email,
    deviceId: offer.device_id,
    expiresAt: new Date(offer.expires_at).toISOString(),
    lastSeenAt: offer.last_seen_at ? new Date(offer.last_seen_at).toISOString() : null,
    createdAt: new Date(offer.created_at).toISOString(),
    updatedAt: new Date(offer.updated_at).toISOString(),
  };
}

function generateOfferCode() {
  let code = "";
  for (let index = 0; index < 6; index += 1) {
    code += OFFER_CODE_ALPHABET[randomInt(0, OFFER_CODE_ALPHABET.length)];
  }
  return code;
}

async function uniqueAgentName(organizationId: string, baseName: string) {
  const existing = await getDb()
    .selectFrom("devices")
    .select(["name"])
    .where("organization_id", "=", organizationId)
    .where("kind", "=", AGENT_KIND)
    .execute();
  const names = new Set(existing.map((device) => device.name.trim().toLowerCase()));
  if (!names.has(baseName.toLowerCase())) return baseName;
  for (let suffix = 2; suffix < 1000; suffix += 1) {
    const candidate = `${baseName} ${suffix}`;
    if (!names.has(candidate.toLowerCase())) return candidate;
  }
  return `${baseName} ${Date.now()}`;
}

export const agentRoutes = new Hono()
  .post("/v1/agents/runs", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = agentRunSchema.parse(await c.req.json().catch(() => ({})));
    const action = (body.action ?? (body.prompt ? "start" : "status")) as AgentAction;
    if ((action === "start" || action === "steer") && !body.prompt) {
      throw new HTTPException(400, { message: `${action} requires prompt` });
    }
    const run = await createAgentRun({
      organizationId: user.organizationId,
      userId: user.userId,
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
    publishOrganization(user.organizationId, {
      type: "agent.run.created",
      source: "agent",
      data: { run: publicAgentRun(run) },
    });
    const finalRun = body.waitMs > 0 ? await waitForAgentRun(run.id, body.waitMs) : run;
    return c.json({ run: publicAgentRun(finalRun) });
  })
  .get("/v1/agents/runs/next", async (c) => {
    const auth = await requireDevice(c.req.raw.headers, new URL(c.req.url));
    const current = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("id", "=", auth.deviceId)
      .where("kind", "=", AGENT_KIND)
      .executeTakeFirst();
    if (!current) throw new HTTPException(404, { message: "Agent not found" });
    const run = await claimNextAgentRun(auth.deviceId, {
      activeRunId: activeRunIdFromHardwareInfo(current.hardware_info),
    });
    return c.json({ run: run ? publicAgentRun(run) : null });
  })
  .post("/v1/agents/runs/:id/complete", async (c) => {
    const auth = await requireDevice(c.req.raw.headers, new URL(c.req.url));
    const body = agentRunCompleteSchema.parse(await c.req.json().catch(() => ({})));
    const completionResult = await completeAgentRun({
      runId: c.req.param("id"),
      agentId: auth.deviceId,
      status: body.status,
      result: body.result,
      error: body.error,
    });
    const run = completionResult.run;
    const completionState = TERMINAL_RUN_STATUSES.has(run.status)
      ? await createAgentCompletionForRun(run)
      : null;
    if (completionResult.changed) {
      publishOrganization(auth.organizationId, {
        type: "agent.run.updated",
        source: "agent",
        data: { run: publicAgentRunEvent(run) },
      });
    }
    if (completionState?.created) {
      publishOrganization(auth.organizationId, {
        type: "agent.completion.created",
        source: "agent",
        data: { completion: publicAgentCompletionEvent(completionState.completion), run: publicAgentRunEvent(run) },
      });
    }
    return c.json({
      run: publicAgentRun(run),
      completion: completionState ? publicAgentCompletion(completionState.completion) : null,
    });
  })
  .post("/v1/agents/runs/:id/approvals", async (c) => {
    const auth = await requireDevice(c.req.raw.headers, new URL(c.req.url));
    const body = agentApprovalCreateSchema.parse(await c.req.json().catch(() => ({})));
    const run = await getDb()
      .selectFrom("agent_runs")
      .selectAll()
      .where("id", "=", c.req.param("id"))
      .where("agent_id", "=", auth.deviceId)
      .executeTakeFirst();
    if (!run) throw new HTTPException(404, { message: "Agent run not found" });
    const approval = await createAgentApproval({
      run,
      codexRequestId: body.codexRequestId,
      codexMethod: body.codexMethod,
      request: body.request,
      expiresAt: new Date(Date.now() + body.expiresMs),
    });
    publishOrganization(auth.organizationId, {
      type: "agent.approval.requested",
      source: "agent",
      data: { approval: publicAgentApproval(approval) },
    });
    return c.json({ approval: publicAgentApproval(approval) });
  })
  .get("/v1/agents/approvals/:id", async (c) => {
    const auth = await requireDevice(c.req.raw.headers, new URL(c.req.url));
    const approval = await getAgentApprovalForDevice(c.req.param("id"), auth.deviceId);
    return c.json({ approval: publicAgentApproval(approval) });
  })
  .post("/v1/agents/approvals/:id/resolve", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = agentApprovalResolveSchema.parse(await c.req.json().catch(() => ({})));
    const approval = await resolveAgentApproval({
      approvalId: c.req.param("id"),
      organizationId: user.organizationId,
      status: body.status,
      response: body.response,
      error: body.error,
    });
    publishOrganization(user.organizationId, {
      type: "agent.approval.resolved",
      source: "agent",
      data: { approval: publicAgentApproval(approval) },
    });
    return c.json({ approval: publicAgentApproval(approval) });
  })
  .get("/v1/agents/approvals", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const approvals = await listAgentApprovals(user.organizationId, {
      status: c.req.query("status"),
      limit: Number(c.req.query("limit") || 50),
    });
    return c.json({ approvals: approvals.map(publicAgentApproval) });
  })
  .get("/v1/agents/completions", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const completions = await listAgentCompletions(user.organizationId, {
      sessionId: c.req.query("sessionId"),
      after: c.req.query("after"),
      limit: Number(c.req.query("limit") || 50),
    });
    return c.json({ completions: completions.map(publicAgentCompletion) });
  })
  .post("/v1/agents/offers", async (c) => {
    const body = offerSchema.parse(await c.req.json().catch(() => ({})));
    const date = now();
    const expiresAt = new Date(date.getTime() + OFFER_TTL_MS);
    const token = body.token?.trim() || opaqueToken("agent_offer");
    const token_hash = tokenHash(token);
    const existing = await getDb()
      .selectFrom("agent_discovery_offers")
      .selectAll()
      .where("offer_token_hash", "=", token_hash)
      .executeTakeFirst();
    if (existing && existing.status === "paired") {
      return c.json({ offer: publicOffer(existing), token });
    }
    if (existing) {
      const offer = await getDb()
        .updateTable("agent_discovery_offers")
        .set({
          role: "agent",
          name: body.name ?? existing.name,
          ...offerMetadata(body),
          status: existing.status === "pending" ? "pending" : "available",
          expires_at: expiresAt,
          last_seen_at: date,
          updated_at: date,
        })
        .where("id", "=", existing.id)
        .returningAll()
        .executeTakeFirstOrThrow();
      return c.json({ offer: publicOffer(offer), token });
    }

    let lastError: unknown = null;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        const offer = await getDb()
          .insertInto("agent_discovery_offers")
          .values({
            id: id("offer"),
            offer_token_hash: token_hash,
            code: generateOfferCode(),
            role: "agent",
            name: body.name ?? "Iris Desktop",
            ...offerMetadata(body),
            status: "available",
            pending_organization_id: null,
            pending_user_id: null,
            pending_user_name: null,
            pending_user_email: null,
            device_id: null,
            expires_at: expiresAt,
            last_seen_at: date,
            created_at: date,
            updated_at: date,
          })
          .returningAll()
          .executeTakeFirstOrThrow();
        return c.json({ offer: publicOffer(offer), token });
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Failed to create desktop offer");
  })
  .get("/v1/agents/offers/self", async (c) => {
    const token = c.req.raw.headers.get("authorization")?.toLowerCase().startsWith("bearer ")
      ? c.req.raw.headers.get("authorization")?.slice(7).trim()
      : c.req.query("token")?.trim();
    if (!token) throw new HTTPException(401, { message: "Missing offer token" });
    const offer = await getDb()
      .selectFrom("agent_discovery_offers")
      .selectAll()
      .where("offer_token_hash", "=", tokenHash(token))
      .executeTakeFirst();
    if (!offer) throw new HTTPException(404, { message: "Desktop offer not found" });
    return c.json({ offer: publicOffer(offer) });
  })
  .get("/v1/agents/offers", async (c) => {
    await requireUser(c.req.raw.headers);
    const offers = await getDb()
      .selectFrom("agent_discovery_offers")
      .selectAll()
      .where("status", "in", ["available", "pending"])
      .where("expires_at", ">", now())
      .orderBy("last_seen_at", "desc")
      .limit(20)
      .execute();
    return c.json({ offers: offers.map(publicOffer) });
  })
  .post("/v1/agents/offers/select", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = selectOfferSchema.parse(await c.req.json().catch(() => ({})));
    const date = now();
    const query = getDb().selectFrom("agent_discovery_offers").selectAll();
    const offer = await (body.offerId
      ? query.where("id", "=", body.offerId)
      : query.where("code", "=", body.code!.trim().toUpperCase().replaceAll(" ", "")))
      .executeTakeFirst();
    if (!offer || offer.status === "paired" || new Date(offer.expires_at).getTime() < Date.now()) {
      throw new HTTPException(404, { message: "Desktop is no longer available" });
    }
    const selected = await getDb()
      .updateTable("agent_discovery_offers")
      .set({
        status: "pending",
        pending_organization_id: user.organizationId,
        pending_user_id: user.userId,
        pending_user_name: user.name,
        pending_user_email: user.email,
        expires_at: new Date(date.getTime() + OFFER_PENDING_TTL_MS),
        updated_at: date,
      })
      .where("id", "=", offer.id)
      .returningAll()
      .executeTakeFirstOrThrow();
    return c.json({ offer: publicOffer(selected) });
  })
  .post("/v1/agents/offers/approve", async (c) => {
    const token = c.req.raw.headers.get("authorization")?.toLowerCase().startsWith("bearer ")
      ? c.req.raw.headers.get("authorization")?.slice(7).trim()
      : null;
    if (!token) throw new HTTPException(401, { message: "Missing offer token" });
    const db = getDb();
    const offer = await db
      .selectFrom("agent_discovery_offers")
      .selectAll()
      .where("offer_token_hash", "=", tokenHash(token))
      .executeTakeFirst();
    if (!offer || offer.status !== "pending" || !offer.pending_user_id || !offer.pending_organization_id) {
      throw new HTTPException(409, { message: "No pending phone request" });
    }
    if (new Date(offer.expires_at).getTime() < Date.now()) {
      throw new HTTPException(410, { message: "Phone request expired" });
    }

    const date = now();
    const deviceToken = opaqueToken("agent");
    const deviceId = id("agent");
    const device = await db.transaction().execute(async (trx) => {
      const inserted = await trx
        .insertInto("devices")
        .values({
          id: deviceId,
          organization_id: offer.pending_organization_id!,
          user_id: offer.pending_user_id!,
          kind: AGENT_KIND,
          product: "iris-mac",
          model: "agent",
          name: await uniqueAgentName(
            offer.pending_organization_id!,
            offer.name || "Iris Agent",
          ),
          status: "online",
          settings: agentSettings(),
          device_serial: null,
          firmware_version: null,
          hardware_info: {
            hostname: offer.hostname,
            platform: offer.platform,
            arch: offer.arch,
            codexVersion: offer.codex_version,
            bridgeUrl: offer.bridge_url,
            active: null,
            activeRun: null,
          },
          last_seen_at: date,
          created_at: date,
          updated_at: date,
        })
        .returningAll()
        .executeTakeFirstOrThrow();
      await trx
        .insertInto("device_credentials")
        .values({
          id: id("cred"),
          device_id: inserted.id,
          token_hash: tokenHash(deviceToken),
          revoked_at: null,
          created_at: date,
        })
        .execute();
      await trx
        .updateTable("agent_discovery_offers")
        .set({
          status: "paired",
          device_id: inserted.id,
          updated_at: date,
        })
        .where("id", "=", offer.id)
        .execute();
      return inserted;
    });
    publishOrganization(offer.pending_organization_id!, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(device) },
    });
    return c.json({ agent: publicAgentDevice(device), token: deviceToken });
  })
  .get("/v1/inventory", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const devices = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .orderBy("created_at", "desc")
      .execute();
    const threads = await listCodexThreads(user.organizationId);
    const runs = await listAgentRuns(user.organizationId, { limit: 50 });
    const completions = await listAgentCompletions(user.organizationId, { limit: 50 });
    const approvals = await listAgentApprovals(user.organizationId, { limit: 50 });
    const agentInventory = publicAgentInventory(devices, threads);
    return c.json({
      devices: devices.map((device) => (device.kind === AGENT_KIND ? publicAgentDevice(device) : publicDevice(device))),
      hardware: devices.filter((device) => device.kind === "hardware").map(publicDevice),
      runs: runs.map(publicAgentRun),
      completions: completions.map(publicAgentCompletion),
      approvals: approvals.map(publicAgentApproval),
      ...agentInventory,
    });
  })
  .get("/v1/agents/threads", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const threads = await listCodexThreads(user.organizationId, { agentId: c.req.query("agentId") });
    return c.json({ threads: threads.map(publicCodexThread) });
  })
  .get("/v1/agents", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const devices = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where("kind", "=", AGENT_KIND)
      .orderBy("created_at", "desc")
      .execute();
    const threads = await listCodexThreads(user.organizationId);
    return c.json(publicAgentInventory(devices, threads));
  })
  .post("/v1/agents/local", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = localAgentSchema.parse(await c.req.json().catch(() => ({})));
    const agent = await upsertLocalAgent(user, body);
    publishOrganization(user.organizationId, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(agent) },
    });
    return c.json({ agent: publicAgentDevice(agent) });
  })
  .patch("/v1/agents/:id", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = updateAgentSchema.parse(await c.req.json().catch(() => ({})));
    const current = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("id", "=", c.req.param("id"))
      .where("organization_id", "=", user.organizationId)
      .where("kind", "=", AGENT_KIND)
      .executeTakeFirst();
    if (!current) throw new HTTPException(404, { message: "Agent not found" });

    const currentSettings = readAgentSettings(current.settings);
    const agent = await getDb()
      .updateTable("devices")
      .set({
        name: body.name ?? current.name,
        model: "agent",
        settings: { ...currentSettings, role: "agent", agentId: null },
        updated_at: now(),
      })
      .where("id", "=", current.id)
      .returningAll()
      .executeTakeFirstOrThrow();
    publishOrganization(user.organizationId, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(agent) },
    });
    return c.json({ agent: publicAgentDevice(agent) });
  })
  .post("/v1/agents/pair", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = pairSchema.parse(await c.req.json().catch(() => ({})));
    const db = getDb();
    const date = now();
    const deviceId = id("agent");
    const pairId = id("pair");
    const token = opaqueToken("agent_pair");
    const expiresAt = new Date(date.getTime() + 15 * 60 * 1000);
    const name = await uniqueAgentName(
      user.organizationId,
      body.name ?? "Iris Agent",
    );
    const device = await db
      .insertInto("devices")
      .values({
        id: deviceId,
        organization_id: user.organizationId,
        user_id: user.userId,
        kind: AGENT_KIND,
        product: "iris-mac",
        model: "agent",
        name,
        status: "pairing",
        settings: agentSettings(),
        device_serial: null,
        firmware_version: null,
        hardware_info: null,
        last_seen_at: null,
        created_at: date,
        updated_at: date,
      })
      .returningAll()
      .executeTakeFirstOrThrow();
    await db
      .insertInto("device_pairing_tokens")
      .values({
        id: pairId,
        organization_id: user.organizationId,
        user_id: user.userId,
        device_id: device.id,
        token_hash: tokenHash(token),
        expires_at: expiresAt,
        claimed_at: null,
        created_at: date,
      })
      .execute();
    publishOrganization(user.organizationId, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(device) },
    });
    return c.json({
      object: "agent_pair",
      id: pairId,
      device: publicAgentDevice(device),
      token,
      expires_at: expiresAt.toISOString(),
    });
  })
  .post("/v1/agents", async (c) => {
    const body = claimSchema.parse(await c.req.json());
    const db = getDb();
    const pair = await db
      .selectFrom("device_pairing_tokens")
      .innerJoin("devices", "devices.id", "device_pairing_tokens.device_id")
      .select([
        "device_pairing_tokens.id as pair_id",
        "device_pairing_tokens.device_id as device_id",
        "device_pairing_tokens.expires_at as expires_at",
        "device_pairing_tokens.claimed_at as claimed_at",
        "devices.user_id as user_id",
        "devices.kind as kind",
      ])
      .where("device_pairing_tokens.token_hash", "=", tokenHash(body.token))
      .executeTakeFirst();
    if (!pair || pair.claimed_at || pair.kind !== AGENT_KIND || new Date(pair.expires_at).getTime() < Date.now()) {
      throw new HTTPException(401, { message: "Invalid pairing token" });
    }
    const date = now();
    const deviceToken = opaqueToken("agent");
    const [device] = await db.transaction().execute(async (trx) => {
      const updated = await trx
        .updateTable("devices")
        .set({
          status: "online",
          hardware_info: agentHardwareInfo(body),
          last_seen_at: date,
          updated_at: date,
        })
        .where("id", "=", pair.device_id)
        .returningAll()
        .execute();
      await trx
        .updateTable("device_pairing_tokens")
        .set({ claimed_at: date })
        .where("id", "=", pair.pair_id)
        .execute();
      await trx
        .insertInto("device_credentials")
        .values({
          id: id("cred"),
          device_id: pair.device_id,
          token_hash: tokenHash(deviceToken),
          revoked_at: null,
          created_at: date,
        })
        .execute();
      return updated;
    });
    if (!device) throw new Error("Failed to claim agent");
    publishOrganization(device.organization_id, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(device) },
    });
    return c.json({ agent: publicAgentDevice(device), token: deviceToken });
  })
  .post("/v1/agents/heartbeat", async (c) => {
    const auth = await requireDevice(c.req.raw.headers, new URL(c.req.url));
    const body = heartbeatSchema.parse(await c.req.json().catch(() => ({})));
    const current = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("id", "=", auth.deviceId)
      .where("kind", "=", AGENT_KIND)
      .executeTakeFirst();
    if (!current) throw new HTTPException(404, { message: "Agent not found" });
    const date = now();
    const currentSettings = readAgentSettings(current.settings);
    const agent = await getDb()
      .updateTable("devices")
      .set({
        status: "online",
        model: "agent",
        settings: { ...currentSettings, available: true },
        hardware_info: agentHardwareInfo(body),
        last_seen_at: date,
        updated_at: date,
      })
      .where("id", "=", auth.deviceId)
      .returningAll()
      .executeTakeFirstOrThrow();
    await syncCodexThreadsFromHeartbeat({
      organizationId: auth.organizationId,
      agentId: auth.deviceId,
      threads: body.threads,
    });
    publishOrganization(auth.organizationId, {
      type: "device.updated",
      source: "agent",
      data: { device: publicAgentDevice(agent) },
    });
    return c.json({ agent: publicAgentDevice(agent) });
  });
