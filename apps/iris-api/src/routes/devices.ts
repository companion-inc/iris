import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { z } from "zod";
import { getDb } from "../db/client.js";
import { requireUser } from "../lib/auth.js";
import { id, now } from "../lib/ids.js";
import { publishOrganization } from "../lib/events.js";
import { publicDevice } from "../lib/shape.js";
import { encryptSecret } from "../lib/secrets.js";
import { opaqueToken, tokenHash } from "../lib/tokens.js";
import type { DeviceRow } from "../db/types.js";
import {
  defaultSoundRecognitionSettings,
  soundRecognitionSettings,
  type SoundRecognitionSettings,
} from "../lib/voice-settings.js";

type DeviceSettings = {
  listeningEnabled: boolean;
  speakerVolume?: number;
  wakeWord: "iris";
  llmBaseUrl: string | null;
  llmModel: string | null;
  llmApiKeyConfigured: boolean;
  llmApiKey?: string | null;
  soundRecognition: SoundRecognitionSettings;
};

const defaultSettings = {
  listeningEnabled: true,
  wakeWord: "iris" as const,
  llmBaseUrl: null as string | null,
  llmModel: null as string | null,
  llmApiKeyConfigured: false,
  soundRecognition: defaultSoundRecognitionSettings(),
} satisfies DeviceSettings;

const pairSchema = z.object({
  name: z.string().trim().min(1).max(80).optional(),
});

const claimSchema = z.object({
  token: z.string().trim().min(1),
  serial: z.string().trim().max(120).optional(),
  firmware: z.string().trim().max(120).optional(),
});

const updateSchema = z.object({
  name: z.string().trim().min(1).max(80).optional(),
  settings: z
    .object({
      listeningEnabled: z.boolean().optional(),
      speakerVolume: z.number().min(0).max(100).optional(),
      llmBaseUrl: z
        .string()
        .trim()
        .max(500)
        .url()
        .refine((value) => value.startsWith("https://") || value.startsWith("http://"), {
          message: "Base URL must use http or https",
        })
        .nullable()
        .optional(),
      llmModel: z.string().trim().min(1).max(120).nullable().optional(),
      llmApiKey: z.string().trim().min(1).max(1000).nullable().optional(),
      soundRecognition: z
        .object({
          enabled: z.boolean().optional(),
        })
        .optional(),
    })
    .optional(),
});

function firstName(user: { name: string | null; email: string }) {
  const fromName = user.name?.trim().split(/\s+/)[0];
  if (fromName) return fromName;
  const fromEmail = user.email.split("@")[0]?.trim();
  return fromEmail || "Your";
}

async function defaultDeviceName(user: { organizationId: string; name: string | null; email: string }) {
  return uniqueDeviceName(user.organizationId, `${firstName(user)}'s Device 1`);
}

async function uniqueDeviceName(organizationId: string, baseName: string) {
  const existing = await getDb()
    .selectFrom("devices")
    .select(["name"])
    .where("organization_id", "=", organizationId)
    .where("kind", "=", "hardware")
    .execute();
  const names = new Set(existing.map((device) => device.name.trim().toLowerCase()));
  const normalizedBase = baseName.trim().toLowerCase();
  if (!names.has(normalizedBase)) return baseName;

  for (let suffix = 2; suffix < 1000; suffix += 1) {
    const candidate = `${baseName} (${suffix})`;
    if (!names.has(candidate.toLowerCase())) return candidate;
  }
  return `${baseName} (${Date.now()})`;
}

async function publicDeviceWithReportedState(device: DeviceRow) {
  return publicDevice(device);
}

function normalizeDeviceSettings(settings: unknown): DeviceSettings {
  const record = settings && typeof settings === "object" ? (settings as Record<string, unknown>) : {};
  return {
    listeningEnabled:
      typeof record.listeningEnabled === "boolean" ? record.listeningEnabled : defaultSettings.listeningEnabled,
    speakerVolume: typeof record.speakerVolume === "number" ? record.speakerVolume : undefined,
    wakeWord: "iris",
    llmBaseUrl: typeof record.llmBaseUrl === "string" ? record.llmBaseUrl : null,
    llmModel: typeof record.llmModel === "string" ? record.llmModel : null,
    llmApiKeyConfigured:
      typeof record.llmApiKeyConfigured === "boolean" ? record.llmApiKeyConfigured : false,
    llmApiKey: typeof record.llmApiKey === "string" ? record.llmApiKey : null,
    soundRecognition: soundRecognitionSettings(record),
  };
}

async function hasDeviceLlmApiKey(deviceId: string) {
  const secret = await getDb()
    .selectFrom("device_secrets")
    .select(["llm_api_key_ciphertext"])
    .where("device_id", "=", deviceId)
    .executeTakeFirst();
  return Boolean(secret?.llm_api_key_ciphertext);
}

async function updateDeviceLlmApiKey(deviceId: string, apiKey: string | null | undefined) {
  if (apiKey === undefined) return;
  if (apiKey === null) {
    await getDb().deleteFrom("device_secrets").where("device_id", "=", deviceId).execute();
    return;
  }
  const encrypted = await encryptSecret(apiKey, { deviceId });
  const date = now();
  await getDb()
    .insertInto("device_secrets")
    .values({
      device_id: deviceId,
      llm_api_key_ciphertext: encrypted,
      created_at: date,
      updated_at: date,
    })
    .onConflict((oc) =>
      oc.column("device_id").doUpdateSet({
        llm_api_key_ciphertext: encrypted,
        updated_at: date,
      }),
    )
    .execute();
}

export const deviceRoutes = new Hono()
  .post("/v1/devices/pair", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = pairSchema.parse(await c.req.json().catch(() => ({})));
    const name = await (body.name
      ? uniqueDeviceName(user.organizationId, body.name)
      : defaultDeviceName(user));
    const db = getDb();
    const date = now();
    const deviceId = id("device");
    const pairId = id("pair");
    const token = opaqueToken("device_pair");
    const expiresAt = new Date(date.getTime() + 15 * 60 * 1000);
    const device = await db
      .insertInto("devices")
      .values({
        id: deviceId,
        organization_id: user.organizationId,
        user_id: user.userId,
        kind: "hardware",
        product: null,
        model: null,
        name,
        status: "pairing",
        settings: defaultSettings,
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
    return c.json({
      object: "pair",
      id: pairId,
      device: publicDevice(device),
      token,
      expires_at: expiresAt.toISOString(),
    });
  })
  .get("/v1/devices", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const devices = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where("kind", "=", "hardware")
      .orderBy("created_at", "desc")
      .execute();
    return c.json({ devices: await Promise.all(devices.map(publicDeviceWithReportedState)) });
  })
  .post("/v1/devices", async (c) => {
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
        "devices.organization_id as organization_id",
        "devices.kind as kind",
        "devices.product as product",
        "devices.model as model",
        "devices.settings as settings",
      ])
      .where("device_pairing_tokens.token_hash", "=", tokenHash(body.token))
      .executeTakeFirst();
    if (!pair || pair.claimed_at || new Date(pair.expires_at).getTime() < Date.now()) {
      throw new HTTPException(401, { message: "Invalid pairing token" });
    }
    const date = now();
    const deviceToken = opaqueToken("device");
    const [device] = await db.transaction().execute(async (trx) => {
      const updated = await trx
        .updateTable("devices")
        .set({
          status: "online",
          device_serial: body.serial ?? null,
          firmware_version: body.firmware ?? null,
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
    if (!device) throw new Error("Failed to claim device");
    publishOrganization(device.organization_id, {
      type: "device.updated",
      source: "hardware",
      data: { device: publicDevice(device) },
    });
    return c.json({ device: publicDevice(device), token: deviceToken });
  })
  .patch("/v1/devices/:id", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const body = updateSchema.parse(await c.req.json());
    const current = await getDb()
      .selectFrom("devices")
      .selectAll()
      .where("id", "=", c.req.param("id"))
      .where("organization_id", "=", user.organizationId)
      .where("kind", "=", "hardware")
      .executeTakeFirst();
    if (!current) throw new HTTPException(404, { message: "Device not found" });
    const { llmApiKey, ...settingsPatch } = body.settings ?? {};
    const { soundRecognition: soundRecognitionPatch, ...otherSettingsPatch } = settingsPatch;
    const currentSettings = normalizeDeviceSettings(current.settings);
    const normalizedPatch: Partial<DeviceSettings> =
      soundRecognitionPatch === undefined
        ? otherSettingsPatch
        : {
            ...otherSettingsPatch,
            soundRecognition: soundRecognitionSettings({ soundRecognition: soundRecognitionPatch }),
          };
    const existingKeyConfigured =
      currentSettings.llmApiKeyConfigured ||
      Boolean(currentSettings.llmApiKey?.trim()) ||
      (llmApiKey === undefined ? await hasDeviceLlmApiKey(current.id) : false);
    const settings = {
      ...currentSettings,
      ...normalizedPatch,
      llmApiKeyConfigured: llmApiKey === undefined ? existingKeyConfigured : llmApiKey !== null,
    };
    delete (settings as { llmApiKey?: string | null }).llmApiKey;
    await updateDeviceLlmApiKey(current.id, llmApiKey);
    const device = await getDb()
      .updateTable("devices")
      .set({
        name: body.name ?? current.name,
        settings,
        status: settings.listeningEnabled
          ? current.status === "muted"
            ? "online"
            : current.status
          : "muted",
        updated_at: now(),
      })
      .where("id", "=", current.id)
      .returningAll()
      .executeTakeFirstOrThrow();
    publishOrganization(user.organizationId, {
      type: "device.updated",
      source: "hardware",
      data: { device: publicDevice(device) },
    });
    return c.json({ device: publicDevice(device) });
  })
  .delete("/v1/devices/:id", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const deviceId = c.req.param("id");
    await getDb()
      .deleteFrom("devices")
      .where("id", "=", deviceId)
      .where("organization_id", "=", user.organizationId)
      .where("kind", "=", "hardware")
      .execute();
    publishOrganization(user.organizationId, {
      type: "device.deleted",
      source: "hardware",
      data: { deviceId },
    });
    return c.json({ ok: true });
  })
  .post("/v1/devices/:id/jobs", async (c) => {
    await requireUser(c.req.raw.headers);
    throw new HTTPException(410, { message: "Remote device jobs have been removed" });
  });
