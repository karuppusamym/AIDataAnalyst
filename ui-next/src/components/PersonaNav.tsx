import type { Persona } from "../lib/types";

/* ---------------------------------------------------------------------------
   UX-1 / module 21 §5: persona navigation bound to the OIDC group contract.

   "Persona chosen in a browser dropdown is a development convenience. In
   production it is derived from identity ... a persona that a user can select
   is a persona that grants nothing, so any capability gated on it would be a
   fake control." (21-experience-shell.md §5)

   This component is the enforcement point for that sentence, so it is kept
   deliberately small and pure -- three render branches, no fetching, no local
   state -- and driven entirely by props so its prod/dev behaviour is directly
   testable without mocking a network call:

     - identityProvider === "OIDC"         -> read-only persona, no <select>
     - identityProvider === "DEVELOPMENT"  -> the manual switcher (unchanged)
     - identityProvider === null (loading) -> nothing yet

   The gate is `identityProvider`, mirrored verbatim from the server's own
   `Settings.identity_provider` (via `GET /v1/me`) -- module 01's existing
   prod-vs-dev-identity switch, not a new one-off flag for this component.
--------------------------------------------------------------------------- */

export const PERSONAS: Persona[] = ["Analyst", "Steward", "Reviewer", "Operator", "Auditor"];

export interface PersonaNavProps {
  /** null while `GET /v1/me` is still in flight -- render nothing rather than
   *  guess, the same fail-closed default module 01's INV-4 applies to identity. */
  identityProvider: "OIDC" | "DEVELOPMENT" | null;
  /** Server-derived in OIDC mode; the dev switcher's current value in DEVELOPMENT
   *  mode; null when OIDC derived no persona for this principal's groups. */
  persona: Persona | null;
  /** Ignored in OIDC mode -- there is nothing for the user to change. */
  onPersonaChange?: (persona: Persona) => void;
}

export function PersonaNav({ identityProvider, persona, onPersonaChange }: PersonaNavProps) {
  if (identityProvider === null) return null;

  if (identityProvider === "OIDC") {
    return (
      <div className="snav__persona" data-testid="persona-nav" data-mode="oidc">
        <span className="snav__plabel" id="persona-label">
          Persona
        </span>
        <span className="snav__pvalue" data-testid="persona-value" aria-labelledby="persona-label">
          {persona ?? "No persona mapped for your groups"}
        </span>
        <span className="snav__pnote">Derived from your OIDC groups</span>
      </div>
    );
  }

  return (
    <div className="snav__persona" data-testid="persona-nav" data-mode="development">
      <label className="snav__plabel" htmlFor="persona">
        Persona
      </label>
      <select
        id="persona"
        data-testid="persona-select"
        value={persona ?? PERSONAS[0]}
        onChange={(e) => onPersonaChange?.(e.target.value as Persona)}
      >
        {PERSONAS.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
      <span className="snav__pnote">Dev only — derived from OIDC in production</span>
    </div>
  );
}
