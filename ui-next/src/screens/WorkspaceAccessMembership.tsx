import { useCallback, useState } from "react";
import type {
  DataSourceRead,
  SourceBindingDecision,
  SourceBindingRead,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
} from "../lib/types";
import { addWorkspaceMember, decideSourceBinding } from "../lib/api";
import { Button, Empty, Field, Pill } from "../components/primitives";
import { FormError, FormSuccess, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Everything scoped to the selected workspace: who belongs to it, and which
   source-binding requests are waiting on a decision.

   These two are one unit because they answer the same question -- who may
   reach what through this workspace -- from the two ends the backend
   separates: membership is granted directly (`add_member`), while a source
   binding is *requested* elsewhere (`AdministrationScreen`'s `BindSourceForm`)
   and only becomes access when a different principal approves it here. A
   reviewer reading one without the other cannot tell whether an approval
   widens access for three people or three hundred.

     Add member       POST /v1/workspaces/{id}/members      (workspace_api.py:160, _ADMIN)
     List members     GET  /v1/workspaces/{id}/members      (workspace_api.py:207, _ANY_MEMBER)
     Decide binding   POST /v1/source-bindings/{id}/decision (workspace_api.py:293, _ADMIN + Reviewer)

   No membership edit or revoke: the legacy screen has no such control either
   (`renderAccess` renders `members` as a read-only table), and `add_member`
   is the only membership write the backend exposes at all.
--------------------------------------------------------------------------- */

const MEMBER_ROLES: WorkspaceMembershipCreate["role"][] = [
  "viewer",
  "analyst",
  "steward",
  "reviewer",
  "workspace_owner",
];
const PRINCIPAL_KINDS: NonNullable<WorkspaceMembershipCreate["principal_kind"]>[] = [
  "HUMAN",
  "AGENT",
  "SERVICE",
];

export function datasourceLabel(datasources: DataSourceRead[], datasourceId: string): string {
  return datasources.find((item) => item.id === datasourceId)?.name ?? datasourceId;
}

export function AddMemberForm({
  workspaceId,
  onAdded,
}: {
  workspaceId: string;
  onAdded: (member: WorkspaceMembershipRead) => void;
}) {
  const [principalId, setPrincipalId] = useState("");
  const [principalKind, setPrincipalKind] =
    useState<NonNullable<WorkspaceMembershipCreate["principal_kind"]>>("HUMAN");
  const [role, setRole] = useState<WorkspaceMembershipCreate["role"]>("analyst");
  const [expiresAt, setExpiresAt] = useState("");
  const action = useSubmitAction<WorkspaceMembershipRead>();

  const valid = principalId.trim().length >= 2;

  const submit = useCallback(async () => {
    if (!valid) return;
    const body: WorkspaceMembershipCreate = {
      principal_id: principalId.trim(),
      principal_kind: principalKind,
      role,
      expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
    };
    const member = await action.run(() => addWorkspaceMember(workspaceId, body));
    if (!member) return;
    setPrincipalId("");
    setExpiresAt("");
    onAdded(member);
  }, [valid, workspaceId, principalId, principalKind, role, expiresAt, action, onAdded]);

  return (
    <form
      className="wsaccess-panel"
      aria-label="Add workspace member"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="wsaccess-panel__head">
        <p className="wsaccess-panel__eyebrow">MEMBERSHIP</p>
        <h2 className="wsaccess-panel__h2">Add member</h2>
      </div>
      <div className="wsaccess-panel__grid">
        <Field label="Principal id">
          <input
            value={principalId}
            onChange={(event) => setPrincipalId(event.target.value)}
            minLength={2}
            required
            placeholder="jordan.reyes"
          />
        </Field>
        <Field label="Principal kind">
          <select
            value={principalKind}
            onChange={(event) =>
              setPrincipalKind(event.target.value as NonNullable<WorkspaceMembershipCreate["principal_kind"]>)
            }
          >
            {PRINCIPAL_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Role">
          <select value={role} onChange={(event) => setRole(event.target.value as WorkspaceMembershipCreate["role"])}>
            {MEMBER_ROLES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Expires (optional)">
          <input type="date" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} />
        </Field>
      </div>
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>
          Added "{action.result.principal_id}" as {action.result.role}.
        </FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!valid || action.submitting}>
        {action.submitting ? "Adding..." : "Add member"}
      </Button>
    </form>
  );
}

export function MembersPanel({ members }: { members: WorkspaceMembershipRead[] }) {
  if (members.length === 0) {
    return <Empty title="No members yet" hint="Add the first member with the form alongside this list." />;
  }
  return (
    <table className="wsaccess-table">
      <thead>
        <tr>
          <th>Principal</th>
          <th>Kind</th>
          <th>Role</th>
          <th>Status</th>
          <th>Expires</th>
        </tr>
      </thead>
      <tbody>
        {members.map((member) => (
          <tr key={member.id}>
            <td>{member.principal_id}</td>
            <td>{member.principal_kind}</td>
            <td>{member.role}</td>
            <td>
              <Pill tone={member.status === "ACTIVE" ? "ok" : "mute"}>{member.status}</Pill>
            </td>
            <td>{member.expires_at ? new Date(member.expires_at).toLocaleDateString() : "Never"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PendingBindingRow({
  binding,
  datasourceName,
  onDecided,
}: {
  binding: SourceBindingRead;
  datasourceName: string;
  onDecided: (binding: SourceBindingRead) => void;
}) {
  const [rationale, setRationale] = useState("");
  // Which button is working, not merely that one is: both stay disabled while
  // a decision is in flight, and only the pressed one changes its label.
  const [pending, setPending] = useState<SourceBindingDecision["decision"] | null>(null);
  const action = useSubmitAction<SourceBindingRead>();

  const decide = useCallback(
    async (decision: SourceBindingDecision["decision"]) => {
      setPending(decision);
      const body: SourceBindingDecision = { decision, valid_for_days: 365, rationale: rationale.trim() };
      const decided = await action.run(() => decideSourceBinding(binding.id, body));
      setPending(null);
      if (decided) onDecided(decided);
    },
    [binding.id, rationale, action, onDecided],
  );

  return (
    <li className="wsaccess-bindingrow">
      <div className="wsaccess-bindingrow__meta">
        <strong>{datasourceName}</strong>
        <small>{binding.purpose}</small>
        <small>Requested by {binding.requested_by}</small>
      </div>
      <input
        className="wsaccess-bindingrow__rationale"
        value={rationale}
        onChange={(event) => setRationale(event.target.value)}
        placeholder="Decision rationale (optional)"
        aria-label={`Rationale for ${datasourceName} binding decision`}
      />
      <div className="wsaccess-bindingrow__actions">
        <Button variant="primary" disabled={action.submitting} onClick={() => void decide("APPROVE")}>
          {pending === "APPROVE" ? "Approving..." : "Approve"}
        </Button>
        <Button disabled={action.submitting} onClick={() => void decide("REJECT")}>
          {pending === "REJECT" ? "Rejecting..." : "Reject"}
        </Button>
      </div>
      {action.error ? <FormError detail={action.error} /> : null}
    </li>
  );
}

export function PendingBindingsPanel({
  bindings,
  datasources,
  onDecided,
}: {
  bindings: SourceBindingRead[];
  datasources: DataSourceRead[];
  onDecided: (binding: SourceBindingRead) => void;
}) {
  if (bindings.length === 0) {
    return (
      <Empty
        title="No pending source-binding requests"
        hint="Every request for this workspace has already been decided."
      />
    );
  }
  return (
    <ul className="wsaccess-bindinglist">
      {bindings.map((binding) => (
        <PendingBindingRow
          key={binding.id}
          binding={binding}
          datasourceName={datasourceLabel(datasources, binding.datasource_id)}
          onDecided={onDecided}
        />
      ))}
    </ul>
  );
}
