import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { completePendingSignIn } from "./lib/oidcClient";
import "./tokens.css";
import "./layout.css";
import "./components/workflow-author.css";

const el = document.getElementById("root");
if (!el) throw new Error("#root is missing from index.html");

/* F06/T07. The authorization response is consumed BEFORE the first render, and
 * outside React, for two reasons that are not style preferences:
 *
 *   - an authorization code is single use, and `StrictMode` runs effects
 *     twice, so a callback handled in an effect would burn the code on the
 *     first call and report a failure on the second;
 *   - rendering first would flash the sign-in screen at a user who is in the
 *     middle of signing in.
 *
 * `completePendingSignIn` resolves whatever happens -- no callback in the URL,
 * a completed exchange, or a failure it has already recorded for the sign-in
 * screen to show -- so the app is always rendered exactly once afterwards.
 *
 * The estate providers (`OrgProvider`, `ScopeProvider`) used to wrap `App`
 * here. They now live inside it, below the auth gate: a build that cannot
 * authenticate must not be issuing organization queries, and one that has just
 * signed in must not still be holding the empty answer it got before it had a
 * token. */
void completePendingSignIn().finally(() => {
  createRoot(el).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
