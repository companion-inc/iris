import { z } from "zod";

export const volumeActionSchema = z.object({
  action: z.enum(["set", "increase", "decrease", "mute", "unmute"]),
  volume: z.number().min(0).max(100).optional(),
  syncDevice: z.boolean().default(true),
});

type VolumeAction = z.infer<typeof volumeActionSchema>["action"];

function clampVolume(value: number) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function currentSpeakerVolume(settings: unknown, hardwareInfo: unknown) {
  if (settings && typeof settings === "object") {
    const value = (settings as Record<string, unknown>).speakerVolume;
    if (typeof value === "number" && Number.isFinite(value)) return clampVolume(value);
  }
  if (hardwareInfo && typeof hardwareInfo === "object") {
    const value = (hardwareInfo as Record<string, unknown>).speakerVolume;
    if (typeof value === "number" && Number.isFinite(value)) return clampVolume(value);
  }
  return 50;
}

export function nextSpeakerVolume(
  currentVolume: number,
  action: VolumeAction,
  volume: number | undefined,
) {
  const step = volume ?? 15;
  if (action === "set") return clampVolume(volume ?? currentVolume);
  if (action === "increase") return clampVolume(currentVolume + step);
  if (action === "decrease") return clampVolume(currentVolume - step);
  if (action === "mute") return 0;
  return clampVolume(volume ?? 50);
}

export async function syncSpeakerVolume(deviceId: string, speakerVolume: number) {
  void deviceId;
  void speakerVolume;
  return false;
}
