import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { loadConfig } from "../config.js";

export function opaqueToken(prefix: string) {
  return `${prefix}_${randomBytes(32).toString("base64url")}`;
}

export function tokenHash(token: string) {
  return createHmac("sha256", loadConfig().tokenSecret).update(token).digest("hex");
}

export function constantEqual(left: string, right: string) {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}

export function bearerToken(headers: Headers, url?: URL) {
  const authorization = headers.get("authorization");
  if (authorization?.toLowerCase().startsWith("bearer ")) {
    return authorization.slice(7).trim();
  }
  return url?.searchParams.get("token")?.trim() || null;
}
