import { useState } from "react";
import type { KillSwitchStateRead } from "../lib/types";
import {
  ApiError,
  engageModelKillSwitch,
  fetchModelKillSwitchState,
  releaseModelKillSwitch,
} from "../lib/api";
import { readDecision, roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, ConfirmDialog, ErrorState, Pill } from "../components/primitives";
import { useAsyncResource, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   The organization kill switch (MG-2, R11-AUD08) -- "stop model use now".

   `AiGovernanceScreen` used to say, in a comment, that this was deliberately
   left out. The reasoning was right about what the switch IS -- a
   single-operator, immediately-effective, audited control, not the model-route
   maker-checker (`ai_governance_api.py`, module 15 section 7) -- and wrong
   about where it belongs: an operator looking at "Models, agents, and
   evaluations" to decide whether AI should be running has no other screen that
   shows whether it has been stopped, and no screen at all from which to stop
   it. The API existed and had no user interface.

   WHAT THIS SHOWS. To every role the read admits: whether the organization
   switch is engaged, by whom, when, and the reason they gave -- or that it was
   released, or never used. Routes stopped one by one are listed too, read-only:
   an organization switch that reads "not engaged" beside a route that is in fact
   halted would be a misleading panel, so the list is the honest half of it.

   WHAT IT LETS YOU DO, and to whom. Engage and Release, PlatformAdmin only, each
   behind a confirmation that says what it does and demands a reason. Both
   controls are shown only when the session is KNOWN to hold the role
   (`roleHolds`, fail-closed): a button that stops the organization's AI is not
   something to offer on a guess while `/v1/me` is in flight.

   WHAT THE SERVER SAYS IS SHOWN AS IT SAYS IT (`failureText`, not a paraphrase):
   the confirmation stays open on a refusal, carrying the server's own sentence.
   There is no maker-checker to explain on release -- the only state refusal is
   409 "kill switch is not currently engaged", which happens when someone else
   already released it; that answer also re-reads the panel so it stops offering
   a Release that no longer applies.

   Scope: the ORGANIZATION switch only. `route_key` is accepted by the API and
   left unused here.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/kill-switch` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.ai_governance_api.list_kill_switch_state`
 * (`Docs/50-security/surface-control-matrix.md`): AgentDeveloper, Auditor,
 * DataSteward, PlatformAdmin, Reviewer, Viewer.
 */
const KILL_SWITCH_READ_ROLES = ["AgentDeveloper", "Auditor", "DataSteward", "PlatformAdmin", "Reviewer", "Viewer"];

/**
 * The roles `POST .../kill-switch/engage` and `.../kill-switch/release` admit.
 *
 * Copied from the matrix rows for `aida.ai_governance_api.engage_kill_switch`
 * and `release_kill_switch`: PlatformAdmin, for both.
 */
const KILL_SWITCH_CHANGE_ROLES = ["PlatformAdmin"];

const listOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;

const stamp = (iso: string | null): string => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "an unknown time");

/** What the organization-scope row says, as the sentences an operator reads. */
function OrganizationState({ row }: { row: KillSwitchStateRead | null }) {
  if (!row) {
    return <p className="aig__kill__detail">This switch has never been used for this organization.</p>;
  }
  if (row.engaged) {
    return (
      <p className="aig__kill__detail">
        Engaged by <b>{row.engaged_by ?? "an unknown principal"}</b> at {stamp(row.engaged_at)}.
        {row.reason ? <> Reason: “{row.reason}”</> : null}
      </p>
    );
  }
  return (
    <p className="aig__kill__detail">
      Last engaged by <b>{row.engaged_by ?? "an unknown principal"}</b> at {stamp(row.engaged_at)}
      {row.reason ? <> (“{row.reason}”)</> : null}; released by <b>{row.released_by ?? "an unknown principal"}</b> at{" "}
      {stamp(row.released_at)}.
    </p>
  );
}

export function KillSwitchPanel({ organizationId }: { organizationId: string }) {
  const session = useSession();
  const roles = session.me?.roles;
  // The read is held while `/v1/me` is in flight and sent only once identity has answered,
  // admitted or unavailable (`readDecision`, `lib/roles.ts`); the server's 403 stays the
  // authority. A session known to be outside the list is never asked.
  const read = readDecision(session, KILL_SWITCH_READ_ROLES);
  const mayRead = read !== "skip";
  const mayChange = roleHolds(roles, KILL_SWITCH_CHANGE_ROLES);
  // "Only a PlatformAdmin can..." is a statement about THIS session, so it waits
  // for identity: said while `/v1/me` is in flight it would tell a PlatformAdmin
  // they cannot do what they are about to be offered.
  const identityKnown = roles !== undefined;

  const state = useAsyncResource<KillSwitchStateRead[]>(
    (signal) => fetchModelKillSwitchState(organizationId, signal),
    [organizationId],
    { enabled: read === "ask" },
  );
  const reloadState = state.reload;

  const [pending, setPending] = useState<"engage" | "release" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const change = useSubmitAction<KillSwitchStateRead>();

  const rows = state.data ?? [];
  const organizationRow = rows.find((row) => row.scope === "ORGANIZATION") ?? null;
  const stoppedRoutes = rows.filter((row) => row.scope === "ROUTE" && row.engaged);
  const engaged = organizationRow?.engaged === true;

  const open = (which: "engage" | "release") => {
    change.reset();
    setNotice(null);
    setPending(which);
  };
  const close = () => {
    change.reset();
    setPending(null);
  };

  const confirm = async (reason: string) => {
    if (!pending) return;
    const which = pending;
    const result = await change.run(async () => {
      try {
        return which === "engage"
          ? await engageModelKillSwitch(organizationId, { reason })
          : await releaseModelKillSwitch(organizationId, { reason });
      } catch (failure) {
        // "not currently engaged": the panel was showing a state that no longer
        // holds, so it is re-read rather than left offering the same button.
        if (failure instanceof ApiError && failure.status === 409) reloadState();
        throw failure;
      }
    });
    if (result === null) return; // the refusal is in `change.error`, shown in the dialog
    setPending(null);
    setNotice(
      which === "engage"
        ? "Kill switch engaged. Model use is stopped for the whole organization."
        : "Kill switch released. Model calls can resume wherever a route is approved and active.",
    );
    reloadState();
  };

  return (
    <article className="aig__panel aig__kill" aria-label="Organization kill switch">
      <div className="aig__panelhead aig__panelhead--padded">
        <div>
          <p className="aig__eyebrow">ORGANIZATION KILL SWITCH</p>
          <h2 className="aig__h2">Stop model use</h2>
        </div>
        {mayRead && state.data ? (
          <Pill tone={engaged ? "bad" : "ok"}>{engaged ? "engaged" : "not engaged"}</Pill>
        ) : null}
      </div>

      {!mayRead ? (
        <p className="aig__kill__detail">
          Not applicable to your roles: only sessions holding {listOr(KILL_SWITCH_READ_ROLES)} can see whether the
          switch is engaged.
        </p>
      ) : state.error ? (
        <ErrorState title="Kill switch state could not be loaded" detail={state.error} onRetry={reloadState} />
      ) : state.loading || !state.data ? (
        <div className="aig__skeleton" role="status">Loading kill switch state…</div>
      ) : (
        <>
          <p className="aig__kill__lead">
            {engaged
              ? "Model use is stopped for the whole organization."
              : stoppedRoutes.length > 0
                ? "Not engaged for the whole organization."
                : "Not engaged. This switch is not blocking model use."}
          </p>
          <OrganizationState row={organizationRow} />
          {stoppedRoutes.length > 0 ? (
            <div className="aig__kill__routes">
              <p className="aig__kill__detail">
                Model use is stopped on these routes (read-only here; a route&rsquo;s switch is engaged and released
                through the API):
              </p>
              <ul>
                {stoppedRoutes.map((route) => (
                  <li key={route.id}>
                    <code>{route.route_key}</code> — engaged by {route.engaged_by ?? "an unknown principal"} at{" "}
                    {stamp(route.engaged_at)}
                    {route.reason ? <>: “{route.reason}”</> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}

      {notice ? <p className="aig__hint" role="status">{notice}</p> : null}

      {mayRead ? (
        mayChange ? (
          <div className="aig__kill__actions">
            {/* A failed read still offers ENGAGE and never RELEASE: the direction
                that stops AI is safe to offer without knowing the current state
                (the server accepts it), the one that starts it again is not. */}
            {engaged ? (
              <Button onClick={() => open("release")} disabled={state.loading}>Release kill switch</Button>
            ) : (
              <Button onClick={() => open("engage")} disabled={state.loading}>Engage kill switch</Button>
            )}
          </div>
        ) : identityKnown ? (
          <p className="aig__hint">Only a PlatformAdmin can engage or release the kill switch.</p>
        ) : null
      ) : null}

      {/* Both are `destructive`, which here means only "a click outside does not
          dismiss": a reason typed for something this consequential should not be
          thrown away by a stray click. */}
      {pending === "engage" ? (
        <ConfirmDialog
          title="Engage the organization kill switch?"
          description="This stops model use for the whole organization: every model call is refused from the very next request until a PlatformAdmin releases the switch. The action is recorded in the audit ledger with your reason."
          confirmLabel="Engage kill switch"
          destructive
          requireReason
          reasonLabel="Reason (recorded in the audit ledger; at least 3 characters)"
          busy={change.submitting}
          error={change.error}
          onConfirm={(reason) => void confirm(reason)}
          onCancel={close}
        />
      ) : null}
      {pending === "release" ? (
        <ConfirmDialog
          title="Release the organization kill switch?"
          description="This lets model calls resume for the whole organization, wherever a route is otherwise approved and active. The release is recorded in the audit ledger with your reason."
          confirmLabel="Release kill switch"
          destructive
          requireReason
          reasonLabel="Reason (recorded in the audit ledger; at least 3 characters)"
          busy={change.submitting}
          error={change.error}
          onConfirm={(reason) => void confirm(reason)}
          onCancel={close}
        />
      ) : null}
    </article>
  );
}
