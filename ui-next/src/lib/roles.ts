/* ---------------------------------------------------------------------------
   What a session's roles say about a surface (tracker R11-AUD01, R11-AUD08).

   THE DEFECT this exists to remove: a screen that reads a route its user's role
   bundle is not admitted to asks anyway, receives a 403 on every load, and then
   either renders an error for a control the user was never meant to have or --
   worse -- renders a zero, because a failed count and an empty count both
   became `0`. `scripts/live_role_matrix.py` proves the API refuses the right
   roles; it cannot tell whether the SCREEN survives being refused. The demo
   rehearsal found two that did not (Home's review-queue count and the context
   product form's ontology picker, both for an AgentDeveloper).

   THE RULE both helpers share: `undefined` is "`GET /v1/me` has not answered
   yet", NOT "no roles". The server's 403 is the authority; the client only
   decides what is worth ASKING for, and it must not turn a slow identity
   request into a permissions problem.

   Where the accepted list comes from. Every caller states its own list beside
   its use and names the row of `Docs/50-security/surface-control-matrix.md`
   it was copied from. That matrix is generated from the application's own
   `require_roles` dependencies, so a list that drifts from it is a list that
   is wrong -- which is why the list lives next to the request it guards and
   not here, where nobody reading the request would see it.

   A LOAD-TIME READ is a different question from a CONTROL, and it has its own
   helper, `readDecision`. `roleAllows` fails open while identity is in flight, so
   a screen that used it to decide whether to ask sent the request before it
   could know, and a session that turned out not to be admitted took a 403 on
   every load -- the demo rehearsal counted it for `sam.agentdev` on Home even
   after the roles were consulted. `readDecision` holds the request while
   identity is still resolving and asks only once it is answered, admitted, or
   known never to come.

   `SourcesScreenAdmin.tsx` carries the original of `roleAllows` (exported since
   R11-S13); it is the same rule and should re-export this one.
--------------------------------------------------------------------------- */

import type { Session } from "./session";

/**
 * Whether a session holding `roles` should be OFFERED something `accepted`
 * guards -- fails OPEN until identity has answered.
 *
 * Use this for what to SHOW: a control or a sentence. For a request a screen
 * makes as it loads, use `readDecision`, which does not ask on a guess.
 */
export function roleAllows(
  roles: readonly string[] | undefined,
  accepted: readonly string[],
): boolean {
  return roles === undefined || roles.some((role) => accepted.includes(role));
}

/**
 * Whether the session is KNOWN to hold one of `accepted` -- fails CLOSED until
 * identity has answered.
 *
 * Use this for a control that must never be offered on a guess: one that stops
 * the organization's model use, say. A moment of not showing it costs nothing;
 * showing it to a session that turns out not to hold the role invites a click
 * the server will refuse (or, worse, an operator's muscle memory for a button
 * that was never theirs).
 */
export function roleHolds(
  roles: readonly string[] | undefined,
  accepted: readonly string[],
): boolean {
  return roles !== undefined && roles.some((role) => accepted.includes(role));
}

/** What a screen should do about a load-time read that `accepted` guards. */
export type ReadDecision = "wait" | "ask" | "skip";

/**
 * Whether to send a read that `accepted` guards, given the session.
 *
 *  - `wait`  identity is still being resolved (`connecting`). Do not ask yet: a session that
 *            turns out not to be admitted would send a request the server has to refuse, once
 *            per load. Show the read as loading and decide when identity answers.
 *  - `ask`   the roles are admitted, or identity is not going to say otherwise (the request
 *            failed, or the build has no identity to consult -- fixtures, a bare render). The
 *            server's 403 stays the authority, so ask.
 *  - `skip`  identity is known and holds none of `accepted`. Send nothing; say the signal does
 *            not apply.
 *
 * Only `connecting` waits, so a failed `/v1/me` or a build with no identity keeps the fail-open
 * behaviour of `roleAllows` instead of leaving the read loading forever.
 */
export function readDecision(
  session: Pick<Session, "state" | "me">,
  accepted: readonly string[],
): ReadDecision {
  if (session.state === "connecting") return "wait";
  return roleAllows(session.me?.roles, accepted) ? "ask" : "skip";
}
