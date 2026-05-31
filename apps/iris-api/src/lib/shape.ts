import { iso } from "./ids.js";
import { accountDisplayName } from "./speaker-profiles.js";
import { soundRecognitionSettings } from "./voice-settings.js";
import type { DeviceRow, TranscriptSegmentRow, VoiceSessionRow } from "../db/types.js";

function publicSettings(settings: unknown) {
  if (!settings || typeof settings !== "object") {
    return { soundRecognition: soundRecognitionSettings(settings) };
  }
  const { llmApiKey, ...safeSettings } = settings as Record<string, unknown>;
  return {
    ...safeSettings,
    llmApiKeyConfigured: typeof llmApiKey === "string" && llmApiKey.trim().length > 0,
    soundRecognition: soundRecognitionSettings(settings),
  };
}

export function publicDevice(device: DeviceRow) {
  return {
    id: device.id,
    userId: device.user_id,
    kind: device.kind,
    product: device.product,
    model: device.model,
    name: device.name,
    settings: publicSettings(device.settings),
    status: device.status,
    deviceSerial: device.device_serial,
    firmwareVersion: device.firmware_version,
    hardwareInfo: device.hardware_info,
    lastSeenAt: iso(device.last_seen_at),
    createdAt: iso(device.created_at),
    updatedAt: iso(device.updated_at),
  };
}

export function publicSegment(
  segment: TranscriptSegmentRow,
  speakerUser?: { name: string | null; email: string } | null,
  options: { includeWords?: boolean } = {},
) {
  return {
    id: segment.id,
    userId: segment.user_id,
    deviceId: segment.device_id,
    sessionId: segment.session_id,
    source: segment.source,
    text: segment.text,
    words: options.includeWords === false ? null : segment.words,
    isInterim: segment.is_interim,
    speakerLabel: segment.speaker_label,
    speakerUserId: segment.speaker_user_id,
    speakerName: speakerUser ? accountDisplayName(speakerUser) : null,
    speakerConfidence: segment.speaker_confidence,
    emotionLabel: segment.emotion_label,
    emotionConfidence: segment.emotion_confidence,
    emotionModel: segment.emotion_model,
    confidence: segment.confidence,
    startedAt: iso(segment.started_at),
    endedAt: iso(segment.ended_at),
    createdAt: iso(segment.created_at),
  };
}

export function publicVoiceSession(
  session: VoiceSessionRow,
  segments: TranscriptSegmentRow[] = [],
  speakerUsers: Map<string, { name: string | null; email: string }> = new Map(),
  options: { includeWords?: boolean } = {},
) {
  return {
    id: session.id,
    userId: session.user_id,
    deviceId: session.device_id,
    roomName: session.room_name,
    status: session.status,
    startedAt: iso(session.started_at),
    endedAt: iso(session.ended_at),
    createdAt: iso(session.created_at),
    updatedAt: iso(session.updated_at),
    segments: segments.map((segment) =>
      publicSegment(
        segment,
        segment.speaker_user_id ? speakerUsers.get(segment.speaker_user_id) : null,
        { includeWords: options.includeWords },
      ),
    ),
  };
}
