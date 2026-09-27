/* ---------------------------------------------------------------------------
   Sign-in for the Excel add-in: the one message its dialog sends back.

   Under OIDC the task pane cannot redirect itself to the identity provider --
   Office will not navigate a task pane off its domain and back -- so sign-in
   runs in an Office dialog on this origin (`excel-addin-auth.html`). That page
   runs the app's existing authorization-code + PKCE flow (`oidcClient.ts`)
   with its own redirect path, then hands the access token to the pane through
   `Office.context.ui.messageParent`, which delivers only to the add-in that
   opened the dialog. The token lives in the pane's memory like the shell's
   does; nothing is written to storage.

   The redirect URI that flow uses, `<origin>/excel-addin-auth.html`, has to be
   registered with the identity provider. That is the one thing this code
   cannot do for a deployment.
--------------------------------------------------------------------------- */

import type { AppConfig } from "../lib/appConfig";

export const ADDIN_AUTH_PATH = "/excel-addin-auth.html";

export type DialogAuthMessage =
  | { readonly kind: "token"; readonly token: string; readonly expiresInSeconds: number | null }
  | { readonly kind: "error"; readonly message: string };

/** The app's OIDC configuration, redirecting back to the dialog page. */
export function dialogAuthConfig(config: AppConfig): AppConfig | null {
  if (!config.oidc) return null;
  return { ...config, oidc: { ...config.oidc, redirectPath: ADDIN_AUTH_PATH } };
}

export function formatDialogMessage(message: DialogAuthMessage): string {
  return JSON.stringify(message);
}

/** Anything that is not a well-formed message reads as a failed sign-in, never
 *  as a token: the pane must not adopt a credential it cannot account for. */
export function parseDialogMessage(raw: string): DialogAuthMessage {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { kind: "error", message: "the sign-in window sent an unreadable reply" };
  }
  if (typeof parsed === "object" && parsed !== null) {
    const candidate = parsed as Record<string, unknown>;
    if (candidate.kind === "token" && typeof candidate.token === "string" && candidate.token) {
      const expires = candidate.expiresInSeconds;
      return {
        kind: "token",
        token: candidate.token,
        expiresInSeconds: typeof expires === "number" && expires >= 0 ? expires : null,
      };
    }
    if (candidate.kind === "error" && typeof candidate.message === "string") {
      return { kind: "error", message: candidate.message };
    }
  }
  return { kind: "error", message: "the sign-in window sent an unexpected reply" };
}
