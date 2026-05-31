import { randomBytes } from "node:crypto";
import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { z } from "zod";
import { loadConfig } from "../config.js";
import { getDb } from "../db/client.js";
import { requireUser, type UserContext } from "../lib/auth.js";
import { id, now } from "../lib/ids.js";
import { logInfo, redactId } from "../lib/log.js";
import { opaqueToken, tokenHash } from "../lib/tokens.js";

const inviteSchema = z.object({
  email: z.string().trim().email().max(320),
  role: z.enum(["admin", "member"]).default("member"),
});

function canManageOrganization(user: UserContext) {
  return user.role === "owner" || user.role === "admin";
}

function requireOrganizationAdmin(user: UserContext) {
  if (!canManageOrganization(user)) {
    throw new HTTPException(403, { message: "Only organization admins can invite members" });
  }
}

function apiOrigin(c: { req: { url: string } }) {
  const configured = loadConfig().publicUrl?.replace(/\/+$/, "");
  if (configured) return configured;
  const url = new URL(c.req.url);
  return `${url.protocol}//${url.host}`;
}

function invitationUrl(c: { req: { url: string } }, token: string) {
  const params = new URLSearchParams({ invitationToken: token });
  return `${apiOrigin(c)}/api/auth/login?${params.toString()}`;
}

function publicInvitation(
  invitation: {
    id: string;
    organization_id: string;
    email: string;
    role: string;
    status: string;
    inviter_user_id: string;
    accepted_user_id: string | null;
    accepted_at: Date | string | null;
    expires_at: Date | string;
    created_at: Date | string;
    updated_at: Date | string;
  },
  c?: { req: { url: string } },
  token?: string,
) {
  return {
    id: invitation.id,
    organizationId: invitation.organization_id,
    email: invitation.email,
    role: invitation.role,
    status: invitation.status,
    inviterUserId: invitation.inviter_user_id,
    acceptedUserId: invitation.accepted_user_id,
    acceptedAt: invitation.accepted_at ? new Date(invitation.accepted_at).toISOString() : null,
    expiresAt: new Date(invitation.expires_at).toISOString(),
    createdAt: new Date(invitation.created_at).toISOString(),
    updatedAt: new Date(invitation.updated_at).toISOString(),
    acceptUrl: c && token ? invitationUrl(c, token) : null,
  };
}

export const organizationRoutes = new Hono()
  .get("/v1/organization", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    const organization = await getDb()
      .selectFrom("organizations")
      .selectAll()
      .where("id", "=", user.organizationId)
      .executeTakeFirst();
    if (!organization) throw new HTTPException(404, { message: "Organization not found" });
    const members = await getDb()
      .selectFrom("organization_members")
      .innerJoin("users", "users.id", "organization_members.user_id")
      .select([
        "organization_members.user_id as user_id",
        "organization_members.role as role",
        "organization_members.created_at as created_at",
        "users.email as email",
        "users.name as name",
        "users.first_name as first_name",
        "users.last_name as last_name",
      ])
      .where("organization_members.organization_id", "=", user.organizationId)
      .orderBy("organization_members.created_at", "asc")
      .execute();
    const invitations = await getDb()
      .selectFrom("organization_invitations")
      .selectAll()
      .where("organization_id", "=", user.organizationId)
      .where("status", "=", "pending")
      .where("expires_at", ">", now())
      .orderBy("created_at", "desc")
      .execute();
    return c.json({
      organization: {
        id: organization.id,
        name: organization.name,
        role: user.role,
        createdAt: new Date(organization.created_at).toISOString(),
        updatedAt: new Date(organization.updated_at).toISOString(),
      },
      members: members.map((member) => ({
        userId: member.user_id,
        email: member.email,
        name: member.name,
        firstName: member.first_name,
        lastName: member.last_name,
        role: member.role,
        createdAt: new Date(member.created_at).toISOString(),
      })),
      invitations: invitations.map((invitation) => publicInvitation(invitation)),
    });
  })
  .post("/v1/organization/invitations", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    requireOrganizationAdmin(user);
    const body = inviteSchema.parse(await c.req.json().catch(() => ({})));
    const existingMember = await getDb()
      .selectFrom("organization_members")
      .innerJoin("users", "users.id", "organization_members.user_id")
      .select(["users.email as email"])
      .where("organization_members.organization_id", "=", user.organizationId)
      .where("users.email", "=", body.email.toLowerCase())
      .executeTakeFirst();
    if (existingMember) throw new HTTPException(409, { message: "User is already a member" });
    const date = now();
    const token = `${opaqueToken("org_invite")}_${randomBytes(8).toString("base64url")}`;
    const invitation = await getDb()
      .insertInto("organization_invitations")
      .values({
        id: id("invite"),
        organization_id: user.organizationId,
        email: body.email.toLowerCase(),
        role: body.role,
        inviter_user_id: user.userId,
        token_hash: tokenHash(token),
        status: "pending",
        accepted_user_id: null,
        accepted_at: null,
        expires_at: new Date(date.getTime() + 14 * 24 * 60 * 60 * 1000),
        created_at: date,
        updated_at: date,
      })
      .returningAll()
      .executeTakeFirstOrThrow();
    logInfo("organization_invitation_created", {
      invitationId: redactId(invitation.id),
      organizationId: redactId(user.organizationId),
    });
    return c.json({ invitation: publicInvitation(invitation, c, token) });
  })
  .post("/v1/organization/invitations/:id/revoke", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    requireOrganizationAdmin(user);
    const invitation = await getDb()
      .updateTable("organization_invitations")
      .set({ status: "revoked", updated_at: now() })
      .where("id", "=", c.req.param("id"))
      .where("organization_id", "=", user.organizationId)
      .where("status", "=", "pending")
      .returningAll()
      .executeTakeFirst();
    if (!invitation) throw new HTTPException(404, { message: "Invitation not found" });
    return c.json({ invitation: publicInvitation(invitation) });
  });
