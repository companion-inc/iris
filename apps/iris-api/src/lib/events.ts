import { getDb } from "../db/client.js";
import { id } from "./ids.js";
import { errorMessage, logWarn, redactId } from "./log.js";

type AppEvent = {
  type: string;
  source: string;
  data: unknown;
};

type IrisEvent = {
  id: string;
  version: 1;
  type: string;
  source: string;
  created_at: string;
  data: unknown;
};

export async function createEventTokenRequest(_userId: string) {
  return null;
}
export function publish(_userId: string, event: AppEvent) {
  return {
    id: id("evt"),
    version: 1,
    type: event.type,
    source: event.source,
    created_at: new Date().toISOString(),
    data: event.data,
  } satisfies IrisEvent;
}

export function publishOrganization(organizationId: string, event: AppEvent) {
  const envelope = publish(organizationId, event);
  void getDb()
    .selectFrom("organization_members")
    .select(["user_id"])
    .where("organization_id", "=", organizationId)
    .execute()
    .catch((error) => {
      logWarn("local_org_event_lookup_failed", {
        organizationId: redactId(organizationId),
        eventId: redactId(envelope.id),
        eventType: envelope.type,
        error: errorMessage(error),
      });
    });
  return envelope;
}
