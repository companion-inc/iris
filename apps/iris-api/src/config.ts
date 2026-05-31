export type IrisApiConfig = {
  port: number;
  environment: "local";
  sqlitePath: string;
  initializeSchema: boolean;
  tokenSecret: string;
  serviceApiKey: string | null;
  publicUrl: string | null;
  auth: {
    mode: "local";
    localUserId: string;
    localEmail: string;
    localName: string;
  };
  voice: {
    url: string | null;
  };
  speakerIdentity: {
    url: string | null;
  };
  defaultOrganization: {
    id: string;
    name: string;
  };
};

function optionalEnv(name: string) {
  const value = process.env[name]?.trim();
  return value ? value : null;
}

function environment() {
  return "local" as const;
}

export function loadConfig(): IrisApiConfig {
  const port = Number.parseInt(optionalEnv("PORT") ?? "4747", 10);
  return {
    port: Number.isFinite(port) ? port : 4747,
    environment: environment(),
    sqlitePath: optionalEnv("IRIS_SQLITE_PATH") ?? ".iris/iris.sqlite",
    initializeSchema: optionalEnv("IRIS_INITIALIZE_SCHEMA") !== "false",
    tokenSecret: optionalEnv("IRIS_TOKEN_SECRET") ?? "iris-development-token-secret",
    serviceApiKey: optionalEnv("IRIS_API_KEY"),
    publicUrl: optionalEnv("IRIS_API_URL"),
    auth: {
      mode: "local",
      localUserId: optionalEnv("IRIS_LOCAL_USER_ID") ?? "user_local",
      localEmail: optionalEnv("IRIS_LOCAL_USER_EMAIL") ?? "local@iris.local",
      localName: optionalEnv("IRIS_LOCAL_USER_NAME") ?? "Local Iris",
    },
    voice: {
      url: optionalEnv("IRIS_VOICE_URL") ?? "http://127.0.0.1:4748",
    },
    speakerIdentity: {
      url: optionalEnv("IRIS_SPEAKER_ID_URL") ?? "http://127.0.0.1:4749",
    },
    defaultOrganization: {
      id: optionalEnv("IRIS_ORGANIZATION_ID") ?? "org_iris",
      name: optionalEnv("IRIS_ORGANIZATION_NAME") ?? "Iris",
    },
  };
}
