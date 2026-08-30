import { useState } from "react";
import { CatalogScreen } from "./screens/CatalogScreen";
import type { Persona } from "./lib/types";
import "./App.css";

/* ---------------------------------------------------------------------------
   The shell.

   Module 21 §5: in production the persona is DERIVED from OIDC group claims.
   The selector below is explicitly a development convenience and is labelled
   as one in the UI — a persona a user can pick grants nothing, so anything
   gated on it would be a fake control. It stays visible during the strangle
   migration precisely so nobody mistakes it for an entitlement.

   Nav marked "legacy" still renders from ui/ (the existing portal). This is
   the strangle seam: the new shell owns routing and chrome from day one, and
   each screen moves across on its own schedule without a cutover.
--------------------------------------------------------------------------- */

const NAV: { id: string; label: string; group: string; ready?: boolean }[] = [
  { id: "home", label: "Overview", group: "Work" },
  { id: "analyst", label: "Ask", group: "Work" },
  { id: "catalog", label: "Catalog", group: "Discover", ready: true },
  { id: "marketplace", label: "Marketplace", group: "Discover" },
  { id: "relationships", label: "Relationships", group: "Discover" },
  { id: "lineage", label: "Lineage", group: "Understand" },
  { id: "semantics", label: "Semantics", group: "Understand" },
  { id: "meaning", label: "Business meaning", group: "Understand" },
  { id: "governance", label: "Review queue", group: "Govern" },
  { id: "quality", label: "Quality", group: "Govern" },
  { id: "audit", label: "Audit ledger", group: "Govern" },
  { id: "sources", label: "Sources", group: "Operate" },
  { id: "operations", label: "Operations", group: "Operate" },
];

const PERSONAS: Persona[] = ["Analyst", "Steward", "Reviewer", "Operator", "Auditor"];

export default function App() {
  const [view, setView] = useState("catalog");
  const [persona, setPersona] = useState<Persona>("Steward");

  const groups = [...new Set(NAV.map((n) => n.group))];
  const current = NAV.find((n) => n.id === view);

  return (
    <div className="shell">
      <nav className="snav" aria-label="Main">
        <div className="snav__brand">
          <span className="snav__mark" aria-hidden="true" />
          <span className="snav__name">Atlas</span>
        </div>

        <div className="snav__persona">
          <label className="snav__plabel" htmlFor="persona">Persona</label>
          <select
            id="persona"
            value={persona}
            onChange={(e) => setPersona(e.target.value as Persona)}
          >
            {PERSONAS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <span className="snav__pnote">Dev only — derived from OIDC in production</span>
        </div>

        {groups.map((g) => (
          <div key={g} className="snav__group">
            <div className="snav__ghead">{g}</div>
            {NAV.filter((n) => n.group === g).map((n) => (
              <button
                key={n.id}
                className="snav__item"
                aria-current={n.id === view ? "page" : undefined}
                onClick={() => setView(n.id)}
              >
                <span>{n.label}</span>
                {n.ready ? null : <span className="snav__legacy">legacy</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>

      <main className="smain">
        {view === "catalog" ? (
          <CatalogScreen />
        ) : (
          <div className="stub">
            <h1>{current?.label}</h1>
            <p>
              Still served by the existing portal. Under the strangle plan this screen
              renders inside the new shell until it is rebuilt — the nav, persona and
              chrome above are already the new ones.
            </p>
            <p className="stub__hint">
              Catalog is the reference implementation. Every screen that moves across
              copies its pattern: URL-held state, one abortable request in flight,
              virtualized list, evidence pane with a permalink.
            </p>
          </div>
        )}
      </main>
    </div>
  );
}
