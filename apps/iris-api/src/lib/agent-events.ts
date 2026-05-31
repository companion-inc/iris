import { HTTPException } from "hono/http-exception";
import { getDb } from "../db/client.js";
import type { AgentApprovalRow, AgentCompletionRow, AgentRunRow } from "../db/types.js";
import { id, iso, now } from "./ids.js";

const EVENT_TEXT_LIMIT = 16_000;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function stringField(value: unknown, key: string) {
  const source = record(value);
  const next = source?.[key];
  return typeof next === "string" && next.trim() ? next.trim() : null;
}

function booleanField(value: unknown, key: string) {
  const source = record(value);
  const next = source?.[key];
  return typeof next === "boolean" ? next : null;
}

function stringListField(value: unknown, key: string, limit = 6) {
  const source = record(value);
  const next = source?.[key];
  if (!Array.isArray(next)) return [];
  return next.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, limit);
}

function boundedLimit(value: number | null | undefined, fallback = 50, max = 100) {
  const next = typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
  return Math.max(1, Math.min(next, max));
}

function deliveryFromRun(run: AgentRunRow) {
  const request = record(run.request);
  const delivery = typeof request?.delivery === "string" ? request.delivery.trim() : "";
  return ["auto", "speak", "save", "silent"].includes(delivery) ? delivery : "auto";
}

function cleanVoiceHandoff(value: unknown) {
  const source = record(value);
  if (!source) return null;
  const handoff: Record<string, unknown> = {};
  for (const key of ["type", "outcome", "summary", "screenState", "followUp"]) {
    const text = stringField(source, key);
    if (text) handoff[key] = text;
  }
  const suggestedSpoken = stringField(source, "suggestedSpoken");
  if (suggestedSpoken) handoff.suggestedSpoken = suggestedSpoken;
  const needsUserAction = booleanField(source, "needsUserAction");
  if (needsUserAction !== null) handoff.needsUserAction = needsUserAction;
  const details = stringListField(source, "details");
  if (details.length) handoff.details = details;
  return Object.keys(handoff).length ? handoff : null;
}

function voiceHandoffFromResult(result: unknown) {
  const direct = cleanVoiceHandoff(record(result)?.voiceHandoff);
  if (direct) return direct;
  return cleanVoiceHandoff(record(record(result)?.next)?.voiceHandoff);
}

function completionContent(result: unknown, error: string | null | undefined) {
  const voice = voiceHandoffFromResult(result);
  const voiceText = stringField(voice, "suggestedSpoken") ?? stringField(voice, "summary") ?? stringField(voice, "followUp");
  if (voiceText) return voiceText;
  const direct = stringField(result, "assistantText") ?? stringField(result, "text") ?? stringField(result, "summary");
  if (direct) return direct;
  const next = record(record(result)?.next);
  const nested = stringField(next, "assistantText") ?? stringField(next, "text") ?? stringField(next, "summary");
  if (nested) return nested;
  return error?.trim() || null;
}

function statusCompletionContent(run: AgentRunRow) {
  if (run.action !== "status") return null;
  if (run.status === "failed") return run.error?.trim() || "I could not reach your computer.";
  const result = record(run.result);
  const active = booleanField(result, "active");
  return active ? "I'm connected to your computer and working." : "I'm connected to your computer.";
}

function actionCompletionContent(run: AgentRunRow) {
  if (run.action !== "interrupt") return null;
  if (run.status === "failed") return run.error?.trim() || "I could not stop the desktop task.";
  return "I stopped the desktop task.";
}

function eventText(value: string | null) {
  if (!value) return value;
  if (value.length <= EVENT_TEXT_LIMIT) return value;
  return `${value.slice(0, EVENT_TEXT_LIMIT)}\n\n[Result truncated. Open Iris to view the full completion.]`;
}

export function publicAgentCompletion(completion: AgentCompletionRow) {
  return {
    id: completion.id,
    organizationId: completion.organization_id,
    userId: completion.user_id,
    runId: completion.run_id,
    sessionId: completion.session_id,
    sourceDeviceId: completion.source_device_id,
    agentId: completion.agent_id,
    threadId: completion.thread_id,
    delivery: completion.delivery,
    status: completion.status,
    content: completion.content,
    voice: voiceHandoffFromResult(completion.result),
    result: completion.result,
    error: completion.error,
    deliveredAt: iso(completion.delivered_at),
    createdAt: iso(completion.created_at),
    updatedAt: iso(completion.updated_at),
  };
}

export function publicAgentCompletionEvent(completion: AgentCompletionRow) {
  return {
    id: completion.id,
    organizationId: completion.organization_id,
    userId: completion.user_id,
    runId: completion.run_id,
    sessionId: completion.session_id,
    sourceDeviceId: completion.source_device_id,
    agentId: completion.agent_id,
    threadId: completion.thread_id,
    delivery: completion.delivery,
    status: completion.status,
    content: eventText(completion.content),
    voice: voiceHandoffFromResult(completion.result),
    result: completion.result,
    error: eventText(completion.error),
    deliveredAt: iso(completion.delivered_at),
    createdAt: iso(completion.created_at),
    updatedAt: iso(completion.updated_at),
  };
}

export function publicAgentApproval(approval: AgentApprovalRow) {
  return {
    id: approval.id,
    organizationId: approval.organization_id,
    userId: approval.user_id,
    runId: approval.run_id,
    sessionId: approval.session_id,
    sourceDeviceId: approval.source_device_id,
    agentId: approval.agent_id,
    threadId: approval.thread_id,
    codexRequestId: approval.codex_request_id,
    codexMethod: approval.codex_method,
    status: approval.status,
    request: approval.request,
    response: approval.response,
    error: approval.error,
    expiresAt: iso(approval.expires_at),
    createdAt: iso(approval.created_at),
    updatedAt: iso(approval.updated_at),
    resolvedAt: iso(approval.resolved_at),
  };
}

export async function createAgentCompletionForRun(run: AgentRunRow) {
  const existing = await getDb()
    .selectFrom("agent_completions")
    .selectAll()
    .where("run_id", "=", run.id)
    .executeTakeFirst();
  if (existing) return { completion: existing, created: false };

  const date = now();
  const completion = await getDb()
    .insertInto("agent_completions")
    .values({
      id: id("agent_completion"),
      organization_id: run.organization_id,
      user_id: run.user_id,
      run_id: run.id,
      session_id: run.session_id,
      source_device_id: run.source_device_id,
      agent_id: run.agent_id,
      thread_id: run.thread_id,
      delivery: deliveryFromRun(run),
      status: run.status,
      content: completionContent(run.result, run.error) ?? actionCompletionContent(run) ?? statusCompletionContent(run),
      result: run.result,
      error: run.error,
      delivered_at: null,
      created_at: date,
      updated_at: date,
    })
    .returningAll()
    .executeTakeFirstOrThrow();
  return { completion, created: true };
}

export async function listAgentCompletions(
  organizationId: string,
  options: { sessionId?: string | null; after?: string | null; limit?: number; undeliveredOnly?: boolean } = {},
) {
  let query = getDb()
    .selectFrom("agent_completions")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .orderBy("created_at", "desc")
    .limit(boundedLimit(options.limit));
  if (options.sessionId) query = query.where("session_id", "=", options.sessionId);
  if (options.after) query = query.where("created_at", ">", new Date(options.after));
  if (options.undeliveredOnly) query = query.where("delivered_at", "is", null);
  return query.execute();
}

export async function markAgentCompletionDelivered(options: {
  completionId: string;
  sessionId: string;
  organizationId: string;
}) {
  const date = now();
  const completion = await getDb()
    .updateTable("agent_completions")
    .set({ delivered_at: date, updated_at: date })
    .where("id", "=", options.completionId)
    .where("organization_id", "=", options.organizationId)
    .where("session_id", "=", options.sessionId)
    .returningAll()
    .executeTakeFirst();
  if (!completion) throw new HTTPException(404, { message: "Agent completion not found" });
  return completion;
}

export async function createAgentApproval(input: {
  run: AgentRunRow;
  codexRequestId?: string | null;
  codexMethod: string;
  request: unknown;
  expiresAt?: Date | null;
}) {
  const date = now();
  return getDb()
    .insertInto("agent_approvals")
    .values({
      id: id("agent_approval"),
      organization_id: input.run.organization_id,
      user_id: input.run.user_id,
      run_id: input.run.id,
      session_id: input.run.session_id,
      source_device_id: input.run.source_device_id,
      agent_id: input.run.agent_id,
      thread_id: input.run.thread_id,
      codex_request_id: input.codexRequestId ?? null,
      codex_method: input.codexMethod,
      status: "pending",
      request: input.request,
      response: null,
      error: null,
      expires_at: input.expiresAt ?? null,
      created_at: date,
      updated_at: date,
      resolved_at: null,
    })
    .returningAll()
    .executeTakeFirstOrThrow();
}

export async function getAgentApprovalForDevice(approvalId: string, agentId: string) {
  const approval = await getDb()
    .selectFrom("agent_approvals")
    .selectAll()
    .where("id", "=", approvalId)
    .where("agent_id", "=", agentId)
    .executeTakeFirst();
  if (!approval) throw new HTTPException(404, { message: "Agent approval not found" });
  return approval;
}

export async function listAgentApprovals(
  organizationId: string,
  options: { status?: string | null; limit?: number } = {},
) {
  let query = getDb()
    .selectFrom("agent_approvals")
    .selectAll()
    .where("organization_id", "=", organizationId)
    .orderBy("created_at", "desc")
    .limit(boundedLimit(options.limit));
  if (options.status) query = query.where("status", "=", options.status);
  return query.execute();
}

export async function resolveAgentApproval(options: {
  approvalId: string;
  organizationId: string;
  status: "approved" | "declined" | "cancelled";
  response?: unknown;
  error?: string | null;
}) {
  const date = now();
  const approval = await getDb()
    .updateTable("agent_approvals")
    .set({
      status: options.status,
      response: options.response ?? null,
      error: options.error?.trim() || null,
      resolved_at: date,
      updated_at: date,
    })
    .where("id", "=", options.approvalId)
    .where("organization_id", "=", options.organizationId)
    .returningAll()
    .executeTakeFirst();
  if (!approval) throw new HTTPException(404, { message: "Agent approval not found" });
  return approval;
}
