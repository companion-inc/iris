import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { HTTPException } from "hono/http-exception";
import { loadConfig } from "./config.js";
import { initializeSchema } from "./db/schema-init.js";
import { errorMessage, logError, logInfo } from "./lib/log.js";
import { startSummaryScheduler } from "./lib/summary-scheduler.js";
import { authRoutes } from "./routes/auth.js";
import { agentRoutes } from "./routes/agents.js";
import { deviceRoutes } from "./routes/devices.js";
import { eventRoutes } from "./routes/events.js";
import { memoryRoutes } from "./routes/memories.js";
import { organizationRoutes } from "./routes/organization.js";
import { speakerProfileRoutes } from "./routes/speaker-profiles.js";
import { summaryRoutes } from "./routes/summaries.js";
import { voiceRoutes } from "./routes/voice.js";
import { transcriptRoutes } from "./routes/transcripts.js";

export const app = new Hono();

app.use(
  "*",
  cors({
    origin: (origin) => {
      if (!origin) return origin;
      if (origin === "http://localhost:1420") return origin;
      if (origin === "tauri://localhost") return origin;
      if (origin === "https://tauri.localhost") return origin;
      return null;
    },
    allowHeaders: ["Authorization", "Content-Type"],
    allowMethods: ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    maxAge: 600,
  }),
);

app.use("*", async (c, next) => {
  const startedAt = Date.now();
  await next();
  logInfo("http_request", {
    method: c.req.method,
    path: new URL(c.req.url).pathname,
    status: c.res.status,
    durationMs: Date.now() - startedAt,
  });
});

app.onError((error, c) => {
  const status = error instanceof HTTPException ? error.status : 500;
  logError("http_error", {
    method: c.req.method,
    path: new URL(c.req.url).pathname,
    status,
    error: errorMessage(error),
  });
  if (error instanceof HTTPException) return error.getResponse();
  return c.json({ error: "Internal Server Error" }, 500);
});

app.get("/health", (c) =>
  c.json({
    ok: true,
    service: "iris-api",
    environment: loadConfig().environment,
  }),
);

app.get("/health/voice", async (c) => {
  const config = loadConfig();
  if (!config.voice.url) {
    logInfo("voice_health_check", { configured: false, reachable: false });
    return c.json(
      {
        ok: false,
        configured: false,
        reachable: false,
        error: "Voice URL is not configured",
      },
      503,
    );
  }

  const voiceUrl = new URL(config.voice.url);
  const healthUrl = new URL("/health", voiceUrl);
  healthUrl.protocol = voiceUrl.protocol === "wss:" ? "https:" : "http:";

  try {
    const response = await fetch(healthUrl, { signal: AbortSignal.timeout(5000) });
    const body = await response.json().catch(() => null);
    const reachable = response.ok;
    logInfo("voice_health_check", {
      configured: true,
      reachable,
      status: response.status,
      voiceProtocol: voiceUrl.protocol.replace(":", ""),
      voiceHost: voiceUrl.host,
      voicePath: voiceUrl.pathname,
    });
    return c.json(
      {
        ok: reachable,
        configured: true,
        reachable,
        status: response.status,
        voiceHost: voiceUrl.host,
        voicePath: voiceUrl.pathname,
        service: typeof body === "object" && body !== null ? (body as { service?: unknown }).service : null,
      },
      reachable ? 200 : 502,
    );
  } catch (error) {
    logError("voice_health_check_failed", {
      configured: true,
      reachable: false,
      error: errorMessage(error),
      voiceProtocol: voiceUrl.protocol.replace(":", ""),
      voiceHost: voiceUrl.host,
      voicePath: voiceUrl.pathname,
    });
    return c.json(
      {
        ok: false,
        configured: true,
        reachable: false,
        error: errorMessage(error),
        voiceHost: voiceUrl.host,
        voicePath: voiceUrl.pathname,
      },
      502,
    );
  }
});

app.get("/", (c) => c.redirect("/health"));
app.route("/", authRoutes);
app.route("/", agentRoutes);
app.route("/", organizationRoutes);
app.route("/", speakerProfileRoutes);
app.route("/", deviceRoutes);
app.route("/", eventRoutes);
app.route("/", memoryRoutes);
app.route("/", voiceRoutes);
app.route("/", transcriptRoutes);
app.route("/", summaryRoutes);

const config = loadConfig();

if (config.initializeSchema) {
  await initializeSchema();
}

startSummaryScheduler();

serve(
  {
    fetch: app.fetch,
    port: config.port,
  },
  (info) => {
    logInfo("server_started", {
      port: info.port,
      environment: config.environment,
      initializeSchema: config.initializeSchema,
    });
  },
);
