import type { Updateable } from "kysely";
import { getDb } from "../db/client.js";
import type { UserMemoryRow, UserMemoryTable } from "../db/types.js";
import { id, iso, now } from "./ids.js";

export type UserMemoryKind = "fact" | "preference" | "instruction";
export type UserMemoryConfidence = "explicit" | "high" | "medium";

const ACTIVE_STATUS = "active";
const DELETED_STATUS = "deleted";

export function normalizeMemoryContent(content: string) {
  return content.replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

export function publicUserMemory(memory: UserMemoryRow) {
  return {
    id: memory.id,
    kind: memory.kind,
    content: memory.content,
    confidence: memory.confidence,
    status: memory.status,
    sourceDeviceId: memory.source_device_id,
    sourceSessionId: memory.source_session_id,
    lastUsedAt: iso(memory.last_used_at),
    createdAt: iso(memory.created_at) ?? "",
    updatedAt: iso(memory.updated_at) ?? "",
  };
}

export async function listUserMemories(options: {
  organizationId: string;
  userId: string;
  limit?: number;
}) {
  const limit = Math.max(1, Math.min(50, options.limit ?? 24));
  return getDb()
    .selectFrom("user_memories")
    .selectAll()
    .where("organization_id", "=", options.organizationId)
    .where("user_id", "=", options.userId)
    .where("status", "=", ACTIVE_STATUS)
    .orderBy("updated_at", "desc")
    .limit(limit)
    .execute();
}

export async function getUserMemory(options: {
  organizationId: string;
  userId: string;
  memoryId: string;
}) {
  return getDb()
    .selectFrom("user_memories")
    .selectAll()
    .where("organization_id", "=", options.organizationId)
    .where("user_id", "=", options.userId)
    .where("id", "=", options.memoryId)
    .where("status", "=", ACTIVE_STATUS)
    .executeTakeFirst();
}

export async function saveUserMemory(options: {
  organizationId: string;
  userId: string;
  sourceDeviceId?: string | null;
  sourceSessionId?: string | null;
  kind: UserMemoryKind;
  content: string;
  confidence: UserMemoryConfidence;
  metadata?: Record<string, unknown>;
}) {
  const date = now();
  const content = options.content.replace(/\s+/g, " ").trim();
  const normalizedContent = normalizeMemoryContent(content);
  return getDb()
    .insertInto("user_memories")
    .values({
      id: id("mem"),
      organization_id: options.organizationId,
      user_id: options.userId,
      source_device_id: options.sourceDeviceId ?? null,
      source_session_id: options.sourceSessionId ?? null,
      kind: options.kind,
      content,
      normalized_content: normalizedContent,
      confidence: options.confidence,
      status: ACTIVE_STATUS,
      metadata: options.metadata ?? {},
      last_used_at: null,
      created_at: date,
      updated_at: date,
    })
    .onConflict((oc) =>
      oc.columns(["organization_id", "user_id", "normalized_content"]).doUpdateSet({
        source_device_id: options.sourceDeviceId ?? null,
        source_session_id: options.sourceSessionId ?? null,
        kind: options.kind,
        content,
        confidence: options.confidence,
        status: ACTIVE_STATUS,
        metadata: options.metadata ?? {},
        updated_at: date,
      }),
    )
    .returningAll()
    .executeTakeFirstOrThrow();
}

export async function updateUserMemory(options: {
  organizationId: string;
  userId: string;
  memoryId: string;
  kind?: UserMemoryKind;
  content?: string;
  confidence?: UserMemoryConfidence;
  metadata?: Record<string, unknown>;
}) {
  const date = now();
  const updates: Updateable<UserMemoryTable> = {
    updated_at: date,
  };
  if (options.kind) updates.kind = options.kind;
  if (options.confidence) updates.confidence = options.confidence;
  if (options.metadata) updates.metadata = options.metadata;
  if (options.content !== undefined) {
    const content = options.content.replace(/\s+/g, " ").trim();
    updates.content = content;
    updates.normalized_content = normalizeMemoryContent(content);
  }

  return getDb()
    .updateTable("user_memories")
    .set(updates)
    .where("organization_id", "=", options.organizationId)
    .where("user_id", "=", options.userId)
    .where("id", "=", options.memoryId)
    .where("status", "=", ACTIVE_STATUS)
    .returningAll()
    .executeTakeFirst();
}

export async function deleteUserMemory(options: {
  organizationId: string;
  userId: string;
  memoryId: string;
}) {
  const date = now();
  return getDb()
    .updateTable("user_memories")
    .set({
      status: DELETED_STATUS,
      updated_at: date,
    })
    .where("organization_id", "=", options.organizationId)
    .where("user_id", "=", options.userId)
    .where("id", "=", options.memoryId)
    .where("status", "=", ACTIVE_STATUS)
    .returningAll()
    .executeTakeFirst();
}
