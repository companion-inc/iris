import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { z } from "zod";
import { requireUser } from "../lib/auth.js";
import {
  deleteUserMemory,
  getUserMemory,
  listUserMemories,
  publicUserMemory,
  saveUserMemory,
  updateUserMemory,
  type UserMemoryConfidence,
  type UserMemoryKind,
} from "../lib/user-memories.js";

const memoryWriteSchema = z.object({
  kind: z.enum(["fact", "preference", "instruction"]).default("fact"),
  content: z.string().trim().min(1).max(500),
  confidence: z.enum(["explicit", "high", "medium"]).default("explicit"),
});

const memoryUpdateSchema = z.object({
  kind: z.enum(["fact", "preference", "instruction"]).optional(),
  content: z.string().trim().min(1).max(500).optional(),
  confidence: z.enum(["explicit", "high", "medium"]).optional(),
});

export const memoryRoutes = new Hono()
  .get("/v1/memories", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const memories = await listUserMemories({
      organizationId: user.organizationId,
      userId: user.userId,
    });
    return c.json({ memories: memories.map(publicUserMemory) });
  })
  .post("/v1/memories", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = memoryWriteSchema.parse(await c.req.json().catch(() => ({})));
    const memory = await saveUserMemory({
      organizationId: user.organizationId,
      userId: user.userId,
      kind: body.kind as UserMemoryKind,
      content: body.content,
      confidence: body.confidence as UserMemoryConfidence,
      metadata: { source: "user" },
    });
    return c.json({ memory: publicUserMemory(memory) });
  })
  .get("/v1/memories/:memoryId", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const memory = await getUserMemory({
      organizationId: user.organizationId,
      userId: user.userId,
      memoryId: c.req.param("memoryId"),
    });
    if (!memory) throw new HTTPException(404, { message: "Memory not found" });
    return c.json({ memory: publicUserMemory(memory) });
  })
  .patch("/v1/memories/:memoryId", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = memoryUpdateSchema.parse(await c.req.json().catch(() => ({})));
    const memory = await updateUserMemory({
      organizationId: user.organizationId,
      userId: user.userId,
      memoryId: c.req.param("memoryId"),
      kind: body.kind as UserMemoryKind | undefined,
      content: body.content,
      confidence: body.confidence as UserMemoryConfidence | undefined,
      metadata: { source: "user" },
    });
    if (!memory) throw new HTTPException(404, { message: "Memory not found" });
    return c.json({ memory: publicUserMemory(memory) });
  })
  .delete("/v1/memories/:memoryId", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const memory = await deleteUserMemory({
      organizationId: user.organizationId,
      userId: user.userId,
      memoryId: c.req.param("memoryId"),
    });
    if (!memory) throw new HTTPException(404, { message: "Memory not found" });
    return c.json({ ok: true, memory: publicUserMemory(memory) });
  });
