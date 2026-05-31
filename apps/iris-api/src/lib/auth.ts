import { HTTPException } from "hono/http-exception";
import { loadConfig } from "../config.js";
import { getDb } from "../db/client.js";
import { bearerToken, tokenHash } from "./tokens.js";

export type UserContext = {
  userId: string;
  email: string;
  name: string | null;
  firstName: string | null;
  lastName: string | null;
  organizationId: string;
  organizationName: string;
  role: string;
};

export type DeviceContext = {
  deviceId: string;
  userId: string;
  organizationId: string;
};

export async function ensureUserRecord(user: {
  id: string;
  email: string;
  name: string | null;
  firstName?: string | null;
  lastName?: string | null;
  preferredOrganizationId?: string | null;
  preferredOrganizationName?: string | null;
  preferredOrganizationRole?: string | null;
}): Promise<UserContext> {
  const db = getDb();
  const config = loadConfig();
  await db
    .insertInto("users")
    .values({
      id: user.id,
      email: user.email,
      name: user.name,
      first_name: user.firstName ?? null,
      last_name: user.lastName ?? null,
      created_at: new Date(),
      updated_at: new Date(),
    })
    .onConflict((oc) =>
      oc.column("id").doUpdateSet({
        email: user.email,
        name: user.name,
        first_name: user.firstName ?? null,
        last_name: user.lastName ?? null,
        updated_at: new Date(),
      }),
    )
    .execute();

  if (user.preferredOrganizationId) {
    const date = new Date();
    const organizationName =
      user.preferredOrganizationName
      ?? (user.preferredOrganizationId === config.defaultOrganization.id
        ? config.defaultOrganization.name
        : "Iris");
    const role = user.preferredOrganizationRole || "member";
    await db.transaction().execute(async (trx) => {
      await trx
        .insertInto("organizations")
        .values({
          id: user.preferredOrganizationId!,
          name: organizationName,
          created_at: date,
          updated_at: date,
        })
        .onConflict((oc) => oc.column("id").doUpdateSet({ updated_at: date }))
        .execute();
      await trx
        .insertInto("organization_members")
        .values({
          organization_id: user.preferredOrganizationId!,
          user_id: user.id,
          role,
          created_at: date,
        })
        .onConflict((oc) => oc.columns(["organization_id", "user_id"]).doNothing())
        .execute();
      await trx
        .updateTable("organization_invitations")
        .set({
          status: "accepted",
          accepted_user_id: user.id,
          accepted_at: date,
          updated_at: date,
        })
        .where("organization_id", "=", user.preferredOrganizationId!)
        .where("email", "=", user.email.trim().toLowerCase())
        .where("status", "=", "pending")
        .where("expires_at", ">", date)
        .execute();
    });
  }

  let memberships = await db
    .selectFrom("organization_members")
    .innerJoin("organizations", "organizations.id", "organization_members.organization_id")
    .select([
      "organization_members.organization_id as organization_id",
      "organization_members.role as role",
      "organizations.name as organization_name",
      "organization_members.created_at as created_at",
    ])
    .where("organization_members.user_id", "=", user.id)
    .orderBy("organization_members.created_at", "asc")
    .execute();

  if (memberships.length === 0) {
    const organizationId = config.defaultOrganization.id;
    await db
      .insertInto("organizations")
      .values({
        id: organizationId,
        name: config.defaultOrganization.name,
        created_at: new Date(),
        updated_at: new Date(),
      })
      .onConflict((oc) => oc.column("id").doUpdateSet({ updated_at: new Date() }))
      .execute();
    await db
      .insertInto("organization_members")
      .values({
        organization_id: organizationId,
        user_id: user.id,
        role: "owner",
        created_at: new Date(),
      })
      .onConflict((oc) => oc.columns(["organization_id", "user_id"]).doNothing())
      .execute();
    memberships = await db
      .selectFrom("organization_members")
      .innerJoin("organizations", "organizations.id", "organization_members.organization_id")
      .select([
        "organization_members.organization_id as organization_id",
        "organization_members.role as role",
        "organizations.name as organization_name",
        "organization_members.created_at as created_at",
      ])
      .where("organization_members.user_id", "=", user.id)
      .orderBy("organization_members.created_at", "asc")
      .execute();
  }

  const selected =
    memberships.find((membership) => membership.organization_id === user.preferredOrganizationId)
    ?? memberships[0];
  if (!selected) throw new HTTPException(500, { message: "No organization membership" });
  return {
    userId: user.id,
    email: user.email,
    name: user.name,
    firstName: user.firstName ?? null,
    lastName: user.lastName ?? null,
    organizationId: selected.organization_id,
    organizationName: selected.organization_name,
    role: selected.role,
  };
}

export async function acceptOrganizationInvitation(options: {
  token: string;
  user: {
    id: string;
    email: string;
  };
}) {
  const db = getDb();
  const invitation = await db
    .selectFrom("organization_invitations")
    .selectAll()
    .where("token_hash", "=", tokenHash(options.token))
    .executeTakeFirst();
  if (!invitation || invitation.status !== "pending") {
    throw new HTTPException(404, { message: "Invitation not found" });
  }
  if (new Date(invitation.expires_at).getTime() < Date.now()) {
    await db
      .updateTable("organization_invitations")
      .set({ status: "expired", updated_at: new Date() })
      .where("id", "=", invitation.id)
      .execute();
    throw new HTTPException(410, { message: "Invitation expired" });
  }
  if (invitation.email.trim().toLowerCase() !== options.user.email.trim().toLowerCase()) {
    throw new HTTPException(403, { message: "Invitation email does not match signed-in user" });
  }

  const date = new Date();
  await db.transaction().execute(async (trx) => {
    await trx
      .insertInto("organization_members")
      .values({
        organization_id: invitation.organization_id,
        user_id: options.user.id,
        role: invitation.role,
        created_at: date,
      })
      .onConflict((oc) =>
        oc.columns(["organization_id", "user_id"]).doUpdateSet({ role: invitation.role }),
      )
      .execute();
    await trx
      .updateTable("organization_invitations")
      .set({
        status: "accepted",
        accepted_user_id: options.user.id,
        accepted_at: date,
        updated_at: date,
      })
      .where("id", "=", invitation.id)
      .execute();
  });
  return invitation.organization_id;
}

export function localAuthEnabled() {
  return true;
}

export async function localUserContext(): Promise<UserContext> {
  const config = loadConfig();
  const name = config.auth.localName || config.auth.localEmail;
  return ensureUserRecord({
    id: config.auth.localUserId,
    email: config.auth.localEmail,
    name,
    preferredOrganizationId: config.defaultOrganization.id,
    preferredOrganizationName: config.defaultOrganization.name,
    preferredOrganizationRole: "owner",
  });
}

export async function sessionUserFromCookie(headers: Headers): Promise<UserContext | null> {
  return localUserContext();
}

export async function requireUser(headers?: Headers): Promise<UserContext> {
  return localUserContext();
}

export async function requireDevice(headers: Headers, url?: URL): Promise<DeviceContext> {
  const token = bearerToken(headers, url);
  if (token) {
    const credential = await getDb()
      .selectFrom("device_credentials")
      .innerJoin("devices", "devices.id", "device_credentials.device_id")
      .select([
        "devices.id as device_id",
        "devices.user_id as user_id",
        "devices.organization_id as organization_id",
        "device_credentials.revoked_at as revoked_at",
      ])
      .where("device_credentials.token_hash", "=", tokenHash(token))
      .executeTakeFirst();
    if (!credential || credential.revoked_at) {
      throw new HTTPException(401, { message: "Invalid device token" });
    }
    return {
      deviceId: credential.device_id,
      userId: credential.user_id,
      organizationId: credential.organization_id,
    };
  }

  const localAgentId = headers.get("x-iris-agent-id")?.trim() || url?.searchParams.get("agentId")?.trim();
  if (!localAgentId) throw new HTTPException(401, { message: "Missing local agent id" });
  const device = await getDb()
    .selectFrom("devices")
    .select(["id", "user_id", "organization_id"])
    .where("id", "=", localAgentId)
    .executeTakeFirst();
  if (!device) throw new HTTPException(404, { message: "Agent not found" });
  return {
    deviceId: device.id,
    userId: device.user_id,
    organizationId: device.organization_id,
  };
}
