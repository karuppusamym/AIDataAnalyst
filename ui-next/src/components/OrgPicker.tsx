import { useOrgSelection } from "../lib/org";

/* ---------------------------------------------------------------------------
   Organization picker for the shell.

   The tenant every screen reads is chosen here once, rather than hard-coded in
   each screen. It reuses the persona block's chrome (`snav__persona`) so it
   sits consistently in the nav. In fixture mode there is a single development
   organization, so the control simply confirms which estate is in view; against
   a live API it lists the real organizations (including a seeded sample estate)
   and lets one be selected.
--------------------------------------------------------------------------- */

export function OrgPicker() {
  const org = useOrgSelection();
  // Outside a provider (bare-rendered tests) there is nothing to pick.
  if (!org) return null;

  const { orgId, organizations, setOrgId, loading, error } = org;

  return (
    <div className="snav__persona" data-testid="org-picker">
      <label className="snav__plabel" htmlFor="org">
        Organization
      </label>
      {error ? (
        <span className="snav__pvalue" data-testid="org-error" role="status">
          Could not load organizations
        </span>
      ) : organizations.length === 0 ? (
        <span className="snav__pvalue" data-testid="org-empty">
          {loading ? "Loading…" : "No organizations yet"}
        </span>
      ) : (
        <select
          id="org"
          data-testid="org-select"
          value={orgId}
          onChange={(e) => setOrgId(e.target.value)}
        >
          {organizations.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
      )}
      <span className="snav__pnote">Estate every screen reads</span>
    </div>
  );
}
