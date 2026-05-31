import { Hono } from "hono";
import {
  acceptOrganizationInvitation,
  localUserContext,
} from "../lib/auth.js";
import { logInfo, redactId } from "../lib/log.js";

export const authRoutes = new Hono()
  .get("/api/auth/login", async (c) => {
    const user = await localUserContext();
    const invitationToken = c.req.query("invitationToken")?.trim();
    if (invitationToken) {
      await acceptOrganizationInvitation({
        token: invitationToken,
        user: { id: user.userId, email: user.email },
      });
    }
    logInfo("auth_login_local", { userId: redactId(user.userId) });
    return c.json({ redirect: false, user: { id: user.userId, email: user.email, name: user.name } });
  })
  .get("/api/auth/callback", async (c) => {
    const user = await localUserContext();
    logInfo("auth_callback_local", { userId: redactId(user.userId) });
    return c.redirect("iris://");
  })
  .get("/api/auth/session", async (c) => {
    const user = await localUserContext();
    logInfo("auth_session_local", { userId: redactId(user.userId) });
    return c.json({
      user: { id: user.userId, email: user.email, name: user.name },
      organization: {
        id: user.organizationId,
        name: user.organizationName,
        role: user.role,
      },
      session: {
        userId: user.userId,
        token: null,
      },
    });
  })
  .post("/api/auth/sign-out", async (c) => {
    logInfo("auth_signout_local");
    return c.json({ ok: true });
  });
