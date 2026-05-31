import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { loadConfig } from "../config.js";

const LOCAL_VERSION = "local:v1";

type SecretContext = {
  deviceId?: string;
  organizationId?: string;
  userId?: string;
  speakerProfileId?: string;
};

function encryptionContext(context?: SecretContext) {
  if (!context) return undefined;
  const entries = Object.entries(context).filter((entry): entry is [string, string] =>
    typeof entry[1] === "string" && entry[1].length > 0,
  );
  return entries.length ? Object.fromEntries(entries) : undefined;
}

function localKey(context?: SecretContext) {
  const config = loadConfig();
  const contextText = JSON.stringify(encryptionContext(context) ?? {});
  return createHash("sha256")
    .update(config.tokenSecret)
    .update("\0iris-local-secret\0")
    .update(contextText)
    .digest();
}

export async function encryptSecret(value: string, context?: SecretContext) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", localKey(context), iv);
  const ciphertext = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return `${LOCAL_VERSION}:${Buffer.concat([iv, tag, ciphertext]).toString("base64url")}`;
}

export async function decryptSecret(value: string | null | undefined, context?: SecretContext) {
  if (!value) return null;
  if (!value.startsWith(`${LOCAL_VERSION}:`)) return null;
  const payload = Buffer.from(value.slice(LOCAL_VERSION.length + 1), "base64url");
  if (payload.length < 29) return null;
  const iv = payload.subarray(0, 12);
  const tag = payload.subarray(12, 28);
  const ciphertext = payload.subarray(28);
  const decipher = createDecipheriv("aes-256-gcm", localKey(context), iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
}
