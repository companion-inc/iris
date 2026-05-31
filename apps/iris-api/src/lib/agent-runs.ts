import type { Kysely, Transaction } from "kysely";
import { HTTPException } from "hono/http-exception";
import { getDb } from "../db/client.js";
import type { AgentRunRow, CodexThreadRow, Database, DeviceRow } from "../db/types.js";
import { id, now } from "./ids.js";

export const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);
const AGENT_ONLINE_TTL_MS = 45_000;

export type AgentAction = "start" | "steer" | "interrupt" | "status";
export type CodexThreadMode = "auto" | "same" | "new";
export type AgentDeliveryMode = "auto" | "speak" | "save" | "silent";
export type AgentRunStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
export type CodexReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh";
export type CodexReasoningSummary = "auto" | "concise" | "detailed" | "none";
export type CodexThinking = {
  effort?: CodexReasoningEffort;
  summary?: CodexReasoningSummary;
};

export type CreateAgentRunInput = {
  organizationId: string;
  userId: string;
  sessionId?: string | null;
  sourceDeviceId?: string | null;
  agentId?: string | null;
  threadId?: string | null;
  thread?: CodexThreadMode | null;
  action: AgentAction;
  prompt?: string | null;
  context?: string | null;
  responseStyle?: string | null;
  delivery?: AgentDeliveryMode | null;
  thinking?: CodexThinking | null;
};

type DbExecutor = Kysely<Database> | Transaction<Database>;

function isFreshAgent(device: DeviceRow) {
  if (device.status !== "online") return false;
  const lastSeenAt = device.last_seen_at ? new Date(device.last_seen_at).getTime() : 0;
  return Number.isFinite(lastSeenAt) && Date.now() - lastSeenAt <= AGENT_ONLINE_TTL_MS;
}

function cleanText(value: string | null | undefined) {
  const text = value?.trim();
  return text || null;
}

function cleanCodexThinking(value: CodexThinking | null | undefined) {
  if (!value) return null;
  return value.effort || value.summary
    ? {
        ...(value.effort ? { effort: value.effort } : {}),
        ...(value.summary ? { summary: value.summary } : {}),
      }
    : null;
}

function boundedLimit(value: number | null | undefined, fallback = 50, max = 100) {
  const next = typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
  return Math.max(1, Math.min(next, max));
}

function publicDate(value: Date | string) {
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}

function titleFromPrompt(prompt: string | null, action: AgentAction) {
  const title = prompt?.replace(/\s+/g, " ").trim();
  if (title) return title.length > 80 ? `${title.slice(0, 77)}...` : title;
  return action === "status" ? "Codex status" : "Codex thread";
}

function resultRecord(result: unknown): Record<string, unknown> | null {
  return result && typeof result === "object" ? (result as Record<string, unknown>) : null;
}

function stringField(record: Record<string, unknown> | null, key: string) {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function extractCodexThreadId(result: unknown) {
  const record = resultRecord(result);
  const direct = stringField(record, "codexThreadId") ?? stringField(record, "nativeThreadId");
  if (direct) return direct;
  const codex = resultRecord(record?.codex);
  const nested = stringField(codex, "codexThreadId") ?? stringField(codex, "threadId");
  if (nested) return nested;
  const next = resultRecord(record?.next);
  const nextThreadId = stringField(next, "codexThreadId") ?? stringField(next, "threadId");
  if (nextThreadId?.startsWith("thr_")) return nextThreadId;
  const legacy = stringField(record, "threadId");
  return legacy?.startsWith("thr_") ? legacy : null;
}

function extractAssistantText(result: unknown) {
  const record = resultRecord(result);
  const direct = stringField(record, "assistantText");
  if (direct) return direct;
  const next = resultRecord(record?.next);
  return stringField(next, "assistantText");
}

export function publicCodexThread(thread: CodexThreadRow) {
  return {
    id: thread.id,
    organizationId: thread.organization_id,
    userId: thread.user_id,
    agentId: thread.agent_id,
    sessionId: thread.session_id,
    sourceDeviceId: thread.source_device_id,
    codexThreadId: thread.codex_thread_id,
    title: thread.title,
    summary: thread.summary,
    status: thread.status,
    currentRunId: thread.current_run_id,
    lastActivityAt: publicDate(thread.last_activity_at),
    createdAt: publicDate(thread.created_at),
    updatedAt: publicDate(thread.updated_at),
  };
}

export function publicAgentRun(run: AgentRunRow) {
  const request = resultRecord(run.request);
  const delivery = stringField(request, "delivery") ?? "auto";
  return {
    id: run.id,
    organizationId: run.organization_id,
    userId: run.user_id,
    sessionId: run.session_id,
    sourceDeviceId: run.source_device_id,
    agentId: run.agent_id,
    threadId: run.thread_id,
    status: run.status,
    action: run.action,
    prompt: run.prompt,
    context: run.context,
    responseStyle: run.response_style,
    delivery,
    request: run.request,
    result: run.result,
    error: run.error,
    createdAt: publicDate(run.created_at),
    updatedAt: publicDate(run.updated_at),
    startedAt: run.started_at ? publicDate(run.started_at) : null,
    completedAt: run.completed_at ? publicDate(run.completed_at) : null,
  };
}

export function publicAgentRunEvent(run: AgentRunRow) {
  const request = resultRecord(run.request);
  const delivery = stringField(request, "delivery") ?? "auto";
  return {
    id: run.id,
    organizationId: run.organization_id,
    userId: run.user_id,
    sessionId: run.session_id,
    sourceDeviceId: run.source_device_id,
    agentId: run.agent_id,
    threadId: run.thread_id,
    status: run.status,
    action: run.action,
    prompt: run.prompt,
    responseStyle: run.response_style,
    delivery,
    error: run.error,
    createdAt: publicDate(run.created_at),
    updatedAt: publicDate(run.updated_at),
    startedAt: run.started_at ? publicDate(run.started_at) : null,
    completedAt: run.completed_at ? publicDate(run.completed_at) : null,
  };
}

export async function resolveAgent(organizationId: string, agentId?: string | null) {
  const devices = await getDb()
    .selectFrom("devices")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .where("kind", "=", "agent")
    .where("status", "!=", "pairing")
    .execute();
  const agents = devices.filter(isFreshAgent);
  if (agentId) {
    const agent = agents.find((candidate) => candidate.id === agentId);
    if (!agent) throw new HTTPException(404, { message: "Desktop agent is not online" });
    return agent;
  }
  if (agents.length === 0) throw new HTTPException(404, { message: "The local Codex bridge is not online" });
  if (agents.length > 1) throw new HTTPException(409, { message: "Multiple local Codex bridges online; pass agentId" });
  return agents[0];
}

async function findThreadById(db: DbExecutor, organizationId: string, agentId: string, threadId: string) {
  const thread = await db
    .selectFrom("codex_threads")
    .selectAll()
    .where("id", "=", threadId)
    .where("organization_id", "=", organizationId)
    .where("agent_id", "=", agentId)
    .executeTakeFirst();
  if (!thread || thread.status === "archived") {
    throw new HTTPException(404, { message: "Codex thread not found" });
  }
  return thread;
}

function newestThreadQuery(db: DbExecutor, organizationId: string, agentId: string) {
  return db
    .selectFrom("codex_threads")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .where("agent_id", "=", agentId)
    .where("status", "!=", "archived")
    .orderBy("last_activity_at", "desc")
    .orderBy("created_at", "desc")
    .limit(1);
}

async function findSessionThread(db: DbExecutor, organizationId: string, agentId: string, sessionId: string) {
  return newestThreadQuery(db, organizationId, agentId).where("session_id", "=", sessionId).executeTakeFirst();
}

async function findActiveThread(db: DbExecutor, organizationId: string, agentId: string) {
  return newestThreadQuery(db, organizationId, agentId).where("status", "=", "active").executeTakeFirst();
}

async function findRecentThread(db: DbExecutor, organizationId: string, agentId: string) {
  return newestThreadQuery(db, organizationId, agentId).executeTakeFirst();
}

async function createCodexThread(
  db: DbExecutor,
  input: CreateAgentRunInput,
  agentId: string,
  prompt: string | null,
  date: Date,
) {
  return db
    .insertInto("codex_threads")
    .values({
      id: id("codex_thread"),
      organization_id: input.organizationId,
      user_id: input.userId,
      agent_id: agentId,
      session_id: cleanText(input.sessionId),
      source_device_id: cleanText(input.sourceDeviceId),
      codex_thread_id: null,
      title: titleFromPrompt(prompt, input.action),
      summary: null,
      status: "idle",
      current_run_id: null,
      last_activity_at: date,
      created_at: date,
      updated_at: date,
    })
    .returningAll()
    .executeTakeFirstOrThrow();
}

async function resolveCodexThread(
  db: DbExecutor,
  input: CreateAgentRunInput,
  agentId: string,
  prompt: string | null,
  date: Date,
) {
  const mode = input.thread ?? "auto";
  const threadId = cleanText(input.threadId);
  const sessionId = cleanText(input.sessionId);

  if (threadId && mode !== "new") {
    return findThreadById(db, input.organizationId, agentId, threadId);
  }
  if (mode === "new") {
    return createCodexThread(db, input, agentId, prompt, date);
  }
  if (input.action === "status") {
    return null;
  }
  if (input.action === "interrupt") {
    return findActiveThread(db, input.organizationId, agentId);
  }
  if (sessionId) {
    const sessionThread = await findSessionThread(db, input.organizationId, agentId, sessionId);
    if (sessionThread) return sessionThread;
  }
  const activeThread = await findActiveThread(db, input.organizationId, agentId);
  if (activeThread) return activeThread;
  if (mode === "same") {
    const recentThread = await findRecentThread(db, input.organizationId, agentId);
    if (recentThread) return recentThread;
  }
  return createCodexThread(db, input, agentId, prompt, date);
}

export async function createAgentRun(input: CreateAgentRunInput) {
  const prompt = cleanText(input.prompt);
  const context = cleanText(input.context);
  const responseStyle = cleanText(input.responseStyle);
  const delivery = input.delivery ?? "auto";
  const thinking = cleanCodexThinking(input.thinking);
  const agent = await resolveAgent(input.organizationId, input.agentId);
  const date = now();
  const threadMode = input.thread ?? "auto";
  return getDb().transaction().execute(async (trx) => {
    const thread = await resolveCodexThread(trx, input, agent.id, prompt, date);
    const runId = id("agent_run");
    const request = {
      agentId: agent.id,
      thread: threadMode,
      threadId: thread?.id ?? null,
      codexThreadId: thread?.codex_thread_id ?? null,
      action: input.action,
      prompt,
      context,
      responseStyle,
      delivery,
      ...(thinking ? { thinking } : {}),
    };
    const run = await trx
      .insertInto("agent_runs")
      .values({
        id: runId,
        organization_id: input.organizationId,
        user_id: input.userId,
        session_id: cleanText(input.sessionId),
        source_device_id: cleanText(input.sourceDeviceId),
        agent_id: agent.id,
        thread_id: thread?.id ?? null,
        status: "queued",
        action: input.action,
        prompt,
        context,
        response_style: responseStyle,
        request,
        result: null,
        error: null,
        created_at: date,
        updated_at: date,
        started_at: null,
        completed_at: null,
      })
      .returningAll()
      .executeTakeFirstOrThrow();
    if (thread) {
      await trx
        .updateTable("codex_threads")
        .set({
          status: "active",
          current_run_id: run.id,
          last_activity_at: date,
          updated_at: date,
        })
        .where("id", "=", thread.id)
        .execute();
    }
    return run;
  });
}

export async function claimNextAgentRun(
  agentId: string,
  _options: { activeRunId?: string | null; staleAfterMs?: number } = {},
) {
  return getDb().transaction().execute(async (trx) => {
    const run = await trx
      .selectFrom("agent_runs")
      .selectAll()
      .where("agent_id", "=", agentId)
      .where("status", "=", "queued")
      .orderBy("created_at", "asc")
      .limit(1)
      .executeTakeFirst();
    const date = now();
    if (!run) return null;
    return trx
      .updateTable("agent_runs")
      .set({
        status: "running",
        started_at: run.started_at ?? date,
        updated_at: date,
      })
      .where("id", "=", run.id)
      .where("status", "=", "queued")
      .returningAll()
      .executeTakeFirst();
  });
}

export async function completeAgentRun(options: {
  runId: string;
  agentId: string;
  status: AgentRunStatus;
  result?: unknown;
  error?: string | null;
}) {
  const date = now();
  const terminal = TERMINAL_RUN_STATUSES.has(options.status);
  return getDb().transaction().execute(async (trx) => {
    const current = await trx
      .selectFrom("agent_runs")
      .selectAll()
      .where("id", "=", options.runId)
      .where("agent_id", "=", options.agentId)
      .executeTakeFirst();
    if (!current) throw new HTTPException(404, { message: "Agent run not found" });
    if (TERMINAL_RUN_STATUSES.has(current.status)) {
      return { run: current, changed: false, becameTerminal: false };
    }
    const updated = await trx
      .updateTable("agent_runs")
      .set({
        status: options.status,
        result: options.result ?? null,
        error: cleanText(options.error),
        completed_at: terminal ? date : null,
        updated_at: date,
      })
      .where("id", "=", options.runId)
      .where("agent_id", "=", options.agentId)
      .where("status", "not in", Array.from(TERMINAL_RUN_STATUSES))
      .returningAll()
      .executeTakeFirst();
    if (!updated) throw new HTTPException(404, { message: "Agent run not found" });
    if (updated.thread_id) {
      const codexThreadId = extractCodexThreadId(options.result);
      const assistantText = extractAssistantText(options.result);
      const result = resultRecord(options.result);
      const bridgeStatus = stringField(result, "status") ?? stringField(resultRecord(result?.next), "status");
      const stillActive = !terminal || bridgeStatus === "running";
      await trx
        .updateTable("codex_threads")
        .set({
          status: stillActive ? "active" : "idle",
          current_run_id: stillActive ? updated.id : null,
          ...(codexThreadId ? { codex_thread_id: codexThreadId } : {}),
          ...(assistantText ? { summary: assistantText.slice(0, 500) } : {}),
          last_activity_at: date,
          updated_at: date,
        })
        .where("id", "=", updated.thread_id)
        .where("agent_id", "=", options.agentId)
        .execute();
    }
    return { run: updated, changed: true, becameTerminal: terminal };
  });
}

export async function getAgentRun(runId: string) {
  return getDb().selectFrom("agent_runs").selectAll().where("id", "=", runId).executeTakeFirst();
}

export async function listCodexThreads(organizationId: string, options: { agentId?: string | null } = {}) {
  let query = getDb()
    .selectFrom("codex_threads")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .where("status", "!=", "archived")
    .orderBy("last_activity_at", "desc")
    .limit(100);
  const agentId = cleanText(options.agentId);
  if (agentId) query = query.where("agent_id", "=", agentId);
  return query.execute();
}

export async function listAgentRuns(organizationId: string, options: { limit?: number; agentId?: string | null } = {}) {
  let query = getDb()
    .selectFrom("agent_runs")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .orderBy("created_at", "desc")
    .limit(boundedLimit(options.limit));
  const agentId = cleanText(options.agentId);
  if (agentId) query = query.where("agent_id", "=", agentId);
  return query.execute();
}

export async function syncCodexThreadsFromHeartbeat(options: {
  organizationId: string;
  agentId: string;
  threads: unknown[] | undefined;
}) {
  if (!options.threads?.length) return;
  const date = now();
  await getDb().transaction().execute(async (trx) => {
    for (const rawThread of options.threads ?? []) {
      const record = resultRecord(rawThread);
      const threadId = stringField(record, "threadId") ?? stringField(record, "id");
      if (!threadId?.startsWith("codex_thread_")) continue;
      const codexThreadId = stringField(record, "codexThreadId");
      const active = record?.active === true;
      const run = resultRecord(record?.run);
      const lastFinished = resultRecord(record?.lastFinished);
      const assistantText = stringField(lastFinished, "assistantText");
      const agentRunId = stringField(run, "agentRunId") ?? stringField(record, "currentRunId");
      await trx
        .updateTable("codex_threads")
        .set({
          status: active ? "active" : "idle",
          current_run_id: active ? agentRunId : null,
          ...(codexThreadId ? { codex_thread_id: codexThreadId } : {}),
          ...(assistantText ? { summary: assistantText.slice(0, 500) } : {}),
          last_activity_at: date,
          updated_at: date,
        })
        .where("id", "=", threadId)
        .where("organization_id", "=", options.organizationId)
        .where("agent_id", "=", options.agentId)
        .execute();
    }
  });
}

export async function waitForAgentRun(runId: string, waitMs: number) {
  const deadline = Date.now() + Math.max(0, Math.min(waitMs, 35000));
  let run = await getAgentRun(runId);
  while (run && !TERMINAL_RUN_STATUSES.has(run.status) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    run = await getAgentRun(runId);
  }
  if (!run) throw new HTTPException(404, { message: "Agent run not found" });
  return run;
}
