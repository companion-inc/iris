export type DeviceTokenClaims = {
  deviceId: string;
  organizationId?: string;
  issuedAt: string;
};

export function bearerToken(headers: Headers, url?: URL) {
  const header = headers.get("authorization") ?? headers.get("Authorization");
  if (header?.toLowerCase().startsWith("bearer ")) return header.slice(7).trim();
  return url?.searchParams.get("token")?.trim() || null;
}

export function createOpaqueDeviceToken() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
