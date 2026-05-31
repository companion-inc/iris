import { getDb } from "../db/client.js";
import { id, now } from "./ids.js";
import type { SpeakerProfileRow } from "../db/types.js";

export function accountDisplayName(user: { name: string | null; email: string }) {
  const name = user.name?.trim();
  if (name) return name;
  const emailName = user.email.split("@")[0]?.trim();
  return emailName || "You";
}

export function publicSpeakerProfile(
  profile: SpeakerProfileRow,
  user?: { name: string | null; email: string },
) {
  return {
    id: profile.id,
    userId: profile.user_id,
    displayName: user ? accountDisplayName(user) : profile.display_name,
    status: profile.status,
    provider: profile.provider,
    sampleCount: profile.sample_count,
    model: profile.model,
    enrolledAt: profile.enrolled_at ? new Date(profile.enrolled_at).toISOString() : null,
    createdAt: new Date(profile.created_at).toISOString(),
    updatedAt: new Date(profile.updated_at).toISOString(),
  };
}

export async function ensureSpeakerProfile(user: {
  userId: string;
  organizationId: string;
  name: string | null;
  email: string;
}) {
  const existing = await getDb()
    .selectFrom("speaker_profiles")
    .selectAll()
    .where("organization_id", "=", user.organizationId)
    .where("user_id", "=", user.userId)
    .executeTakeFirst();
  if (existing) return existing;

  const date = now();
  return getDb()
    .insertInto("speaker_profiles")
    .values({
      id: id("speaker"),
      organization_id: user.organizationId,
      user_id: user.userId,
      display_name: accountDisplayName(user),
      status: "not_registered",
      provider: "speechbrain-ecapa",
      sample_count: 0,
      model: "speechbrain/spkrec-ecapa-voxceleb",
      embedding_ciphertext: null,
      enrolled_at: null,
      created_at: date,
      updated_at: date,
    })
    .onConflict((oc) =>
      oc.columns(["organization_id", "user_id"]).doUpdateSet({ updated_at: date }),
    )
    .returningAll()
    .executeTakeFirstOrThrow();
}

export async function listOrganizationSpeakerProfiles(organizationId: string) {
  const members = await getDb()
    .selectFrom("organization_members")
    .innerJoin("users", "users.id", "organization_members.user_id")
    .leftJoin("speaker_profiles", (join) =>
      join
        .onRef("speaker_profiles.user_id", "=", "organization_members.user_id")
        .onRef("speaker_profiles.organization_id", "=", "organization_members.organization_id"),
    )
    .select([
      "organization_members.user_id as user_id",
      "users.email as email",
      "users.name as name",
      "speaker_profiles.id as profile_id",
      "speaker_profiles.display_name as display_name",
      "speaker_profiles.status as status",
      "speaker_profiles.provider as provider",
      "speaker_profiles.sample_count as sample_count",
      "speaker_profiles.model as model",
      "speaker_profiles.enrolled_at as enrolled_at",
      "speaker_profiles.created_at as created_at",
      "speaker_profiles.updated_at as updated_at",
    ])
    .where("organization_members.organization_id", "=", organizationId)
    .orderBy("organization_members.created_at", "asc")
    .execute();

  return members.map((member) => ({
    userId: member.user_id,
    email: member.email,
    name: member.name,
    profile: member.profile_id
      ? {
          id: member.profile_id,
          userId: member.user_id,
          displayName: accountDisplayName(member),
          status: member.status ?? "not_registered",
          provider: member.provider ?? "speechbrain-ecapa",
          sampleCount: member.sample_count ?? 0,
          model: member.model,
          enrolledAt: member.enrolled_at ? new Date(member.enrolled_at).toISOString() : null,
          createdAt: member.created_at ? new Date(member.created_at).toISOString() : "",
          updatedAt: member.updated_at ? new Date(member.updated_at).toISOString() : "",
        }
      : null,
  }));
}
