import { Hono } from "hono";
import { requireUser } from "../lib/auth.js";
import { logInfo, redactId } from "../lib/log.js";
import { HTTPException } from "hono/http-exception";

export const eventRoutes = new Hono()
  .post("/v1/events/token", async (c) => {
    const user = await requireUser(c.req.raw.headers);
    logInfo("events_token_created", {
      userId: redactId(user.userId),
    });
    throw new HTTPException(410, { message: "Hosted realtime events have been removed" });
  });
