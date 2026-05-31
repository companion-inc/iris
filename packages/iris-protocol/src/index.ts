export type SessionSource = "hardware" | "hub";

export type DeviceIdentity = {
  deviceId: string;
  organizationId?: string;
};

export type DeviceKind = "hardware" | "hub" | string;

export type DeviceStatus = "pairing" | "online" | "listening" | "offline" | "muted";

export type HardwareDeviceSettings = {
  listeningEnabled: boolean;
  speakerVolume: number;
  wakeWord: "iris";
  llmBaseUrl?: string | null;
  llmModel?: string | null;
  llmApiKey?: string | null;
  llmApiKeyConfigured?: boolean;
  soundRecognition?: SoundRecognitionSettings;
};

export type SoundRecognitionSettings = {
  enabled: boolean;
  watches: SoundRecognitionWatch[];
};

export type SoundRecognitionWatch = {
  id: string;
  label: string;
  enabled: boolean;
  threshold: number | null;
  behavior: "log" | "notify" | "prompt";
  prompt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
};

export type Device = DeviceIdentity & {
  kind: DeviceKind;
  product: string | null;
  model: string | null;
  name: string;
  status: DeviceStatus;
  settings: Record<string, unknown>;
  deviceSerial: string | null;
  firmwareVersion: string | null;
  hardwareInfo: Record<string, unknown> | null;
  lastSeenAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type HardwareDevice = Device & {
  kind: "hardware";
  settings: HardwareDeviceSettings;
};

export type DevicePairingToken = {
  object: "pairing_token";
  id: string;
  token: string;
  expiresAt: string;
  device: HardwareDevice;
};
