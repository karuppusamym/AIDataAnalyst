/* The Excel add-in's sign-in dialog page (`excel-addin-auth.html`).
 *
 * First visit: start the app's authorization-code + PKCE flow, redirecting back
 * here. Second visit (the identity provider's redirect): complete it, then send
 * the access token to the task pane and let Office close the window. See
 * `authMessage.ts` for why sign-in happens in a dialog at all. */

import { APP_CONFIG } from "../lib/appConfig";
import { accessTokenExpiresAt, getAccessToken } from "../lib/authSession";
import { beginSignIn, completePendingSignIn } from "../lib/oidcClient";
import { dialogAuthConfig, formatDialogMessage, type DialogAuthMessage } from "./authMessage";

interface DialogOffice {
  onReady(): Promise<unknown>;
  context: { ui: { messageParent(message: string): void } };
}

function office(): DialogOffice | undefined {
  return (globalThis as { Office?: DialogOffice }).Office;
}

function status(text: string): void {
  const element = document.getElementById("status");
  if (element) element.textContent = text;
}

function send(message: DialogAuthMessage): void {
  const host = office();
  if (!host) {
    status("This page only works inside the Atlas add-in's sign-in window.");
    return;
  }
  host.context.ui.messageParent(formatDialogMessage(message));
}

async function run(): Promise<void> {
  await office()?.onReady();
  const config = dialogAuthConfig(APP_CONFIG);
  if (!config) {
    send({ kind: "error", message: "this build has no identity provider configured" });
    return;
  }
  const outcome = await completePendingSignIn(config);
  if (outcome.kind === "none") {
    status("Redirecting to your identity provider…");
    await beginSignIn(config);
    return;
  }
  if (outcome.kind === "failed") {
    send({ kind: "error", message: outcome.message });
    return;
  }
  const token = getAccessToken();
  if (!token) {
    send({ kind: "error", message: "the identity provider returned no access token" });
    return;
  }
  const expiresAt = accessTokenExpiresAt();
  send({
    kind: "token",
    token,
    expiresInSeconds:
      expiresAt === null ? null : Math.max(0, Math.floor((expiresAt - Date.now()) / 1000)),
  });
  status("Signed in. You can close this window.");
}

void run().catch((cause: unknown) =>
  send({ kind: "error", message: cause instanceof Error ? cause.message : String(cause) }),
);
