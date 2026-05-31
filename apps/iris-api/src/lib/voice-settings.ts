import { getDb } from "../db/client.js";

export type SoundRecognitionBehavior = "log" | "notify" | "prompt";

export type SoundRecognitionWatch = {
  id: string;
  label: string;
  enabled: boolean;
  threshold: number | null;
  behavior: SoundRecognitionBehavior;
  prompt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
};

export type SoundRecognitionSettings = {
  enabled: boolean;
  watches: SoundRecognitionWatch[];
};

const DEFAULT_SOUND_RECOGNITION_WATCHES: SoundRecognitionWatch[] = [
  {
    id: "default_doorbell",
    label: "doorbell",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_knock",
    label: "knock",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_smoke_alarm",
    label: "smoke alarm",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_fire_alarm",
    label: "fire alarm",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_glass_breaking",
    label: "glass breaking",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_dog_bark",
    label: "dog bark",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_baby_cry",
    label: "baby cry",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_beep",
    label: "beep",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_siren",
    label: "siren",
    enabled: true,
    threshold: 0.65,
    behavior: "log",
    prompt: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: "default_sneeze",
    label: "sneeze",
    enabled: true,
    threshold: 0.75,
    behavior: "prompt",
    prompt: 'Say "bless you."',
    createdAt: null,
    updatedAt: null,
  },
];

function defaultSoundRecognitionWatches(): SoundRecognitionWatch[] {
  return DEFAULT_SOUND_RECOGNITION_WATCHES.map((watch) => ({ ...watch }));
}

export function defaultSoundRecognitionSettings(): SoundRecognitionSettings {
  return { enabled: false, watches: defaultSoundRecognitionWatches() };
}

function addVocabularyTerm(terms: string[], seen: Set<string>, value: string | null | undefined) {
  const term = value?.replace(/\s+/g, " ").trim();
  if (!term) return;
  const key = term.toLocaleLowerCase();
  if (seen.has(key)) return;
  seen.add(key);
  terms.push(term);
}

function nameFallbackParts(name: string | null) {
  const parts = name?.trim().split(/\s+/).filter(Boolean) ?? [];
  if (parts.length === 0) return { firstName: null, lastName: null };
  return {
    firstName: parts[0] ?? null,
    lastName: parts.length > 1 ? parts.slice(1).join(" ") : null,
  };
}

export async function voiceVocabulary(organizationId: string) {
  const terms: string[] = [];
  const seen = new Set<string>();
  addVocabularyTerm(terms, seen, "Iris");

  const rows = await getDb()
    .selectFrom("organization_members")
    .innerJoin("users", "users.id", "organization_members.user_id")
    .innerJoin("organizations", "organizations.id", "organization_members.organization_id")
    .select([
      "users.name as name",
      "users.first_name as first_name",
      "users.last_name as last_name",
      "organizations.name as organization_name",
    ])
    .where("organization_members.organization_id", "=", organizationId)
    .execute();

  for (const row of rows) {
    const fallback = nameFallbackParts(row.name);
    addVocabularyTerm(terms, seen, row.first_name ?? fallback.firstName);
    addVocabularyTerm(terms, seen, row.last_name ?? fallback.lastName);
    addVocabularyTerm(terms, seen, row.organization_name);
  }

  return terms;
}

export function voiceLlmSettings(settings: unknown) {
  if (!settings || typeof settings !== "object") {
    return { baseUrl: null, model: null };
  }
  const record = settings as Record<string, unknown>;
  return {
    baseUrl:
      typeof record.llmBaseUrl === "string" && record.llmBaseUrl.trim()
        ? record.llmBaseUrl.trim()
        : null,
    model:
      typeof record.llmModel === "string" && record.llmModel.trim()
        ? record.llmModel.trim()
        : null,
  };
}

export function listeningEnabled(settings: unknown) {
  if (!settings || typeof settings !== "object") return true;
  const value = (settings as Record<string, unknown>).listeningEnabled;
  return typeof value === "boolean" ? value : true;
}

export function soundRecognitionSettings(settings: unknown): SoundRecognitionSettings {
  if (!settings || typeof settings !== "object") return defaultSoundRecognitionSettings();
  const value = (settings as Record<string, unknown>).soundRecognition;
  if (!value || typeof value !== "object") return defaultSoundRecognitionSettings();
  const record = value as Record<string, unknown>;
  const enabled = typeof record.enabled === "boolean" ? record.enabled : false;
  return { enabled, watches: defaultSoundRecognitionWatches() };
}
