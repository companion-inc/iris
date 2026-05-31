import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { z } from "zod";
import { loadConfig } from "../config.js";
import { getDb } from "../db/client.js";
import { requireUser } from "../lib/auth.js";
import { now } from "../lib/ids.js";
import { encryptSecret } from "../lib/secrets.js";
import {
  ensureSpeakerProfile,
  listOrganizationSpeakerProfiles,
  publicSpeakerProfile,
} from "../lib/speaker-profiles.js";

const updateSpeakerProfileSchema = z.object({
  enabled: z.boolean().optional(),
});

const enrollmentSchema = z.object({
  samples: z
    .array(
      z.object({
        audioBase64: z.string().min(1),
        mimeType: z.string().trim().min(1).max(120),
        durationMs: z.number().int().min(500).max(120000).optional(),
      }),
    )
    .min(1)
    .max(8),
});

const speakerIdEnrollResponseSchema = z.object({
  embedding: z.array(z.number()).min(1),
  sampleCount: z.number().int().min(1),
  model: z.string().trim().min(1).max(160).optional(),
});

async function enrollSpeaker(samples: z.infer<typeof enrollmentSchema>["samples"]) {
  const speakerIdUrl = loadConfig().speakerIdentity.url?.replace(/\/+$/, "");
  if (!speakerIdUrl) {
    throw new HTTPException(503, { message: "Speaker recognition service is not configured" });
  }
  const response = await fetch(`${speakerIdUrl}/v1/enroll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ samples }),
    signal: AbortSignal.timeout(45000),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new HTTPException(502, { message: text || "Speaker recognition enrollment failed" });
  }
  return speakerIdEnrollResponseSchema.parse(await response.json());
}

export const speakerProfileRoutes = new Hono()
  .get("/v1/speaker-profiles", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const members = await listOrganizationSpeakerProfiles(user.organizationId);
    return c.json({ members });
  })
  .get("/v1/speaker-profile", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const profile = await ensureSpeakerProfile(user);
    return c.json({ profile: publicSpeakerProfile(profile, user) });
  })
  .patch("/v1/speaker-profile", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = updateSpeakerProfileSchema.parse(await c.req.json().catch(() => ({})));
    const current = await ensureSpeakerProfile(user);
    const profile = await getDb()
      .updateTable("speaker_profiles")
      .set({
        status:
          body.enabled === undefined
            ? current.status
            : body.enabled
              ? current.status === "disabled"
                ? "not_registered"
                : current.status
              : "disabled",
        updated_at: now(),
      })
      .where("id", "=", current.id)
      .where("organization_id", "=", user.organizationId)
      .returningAll()
      .executeTakeFirstOrThrow();
    return c.json({ profile: publicSpeakerProfile(profile, user) });
  })
  .post("/v1/speaker-profile/enroll", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = enrollmentSchema.parse(await c.req.json());
    const current = await ensureSpeakerProfile(user);
    const enrollment = await enrollSpeaker(body.samples);
    const date = now();
    const encryptedEmbedding = await encryptSecret(JSON.stringify(enrollment.embedding), {
      organizationId: user.organizationId,
      userId: user.userId,
      speakerProfileId: current.id,
    });
    const profile = await getDb()
      .updateTable("speaker_profiles")
      .set({
        status: "registered",
        provider: "speechbrain-ecapa",
        sample_count: enrollment.sampleCount,
        model: enrollment.model ?? "speechbrain/spkrec-ecapa-voxceleb",
        embedding_ciphertext: encryptedEmbedding,
        enrolled_at: date,
        updated_at: date,
      })
      .where("id", "=", current.id)
      .where("organization_id", "=", user.organizationId)
      .returningAll()
      .executeTakeFirstOrThrow();
    return c.json({ profile: publicSpeakerProfile(profile, user) });
  });
