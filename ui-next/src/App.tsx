import { useEffect, useState } from "react";
import { CatalogScreen } from "./screens/CatalogScreen";
import { ReviewQueueScreen } from "./screens/ReviewQueueScreen";
import { MarketplaceScreen } from "./screens/MarketplaceScreen";
import { LineageRefusalScreen } from "./screens/LineageRefusalScreen";
import { StudioChangeSetsScreen } from "./screens/StudioChangeSetsScreen";
import { NarratedLineageScreen } from "./screens/NarratedLineageScreen";
import { AskScreen } from "./screens/AskScreen";
import { RelationshipsScreen } from "./screens/RelationshipsScreen";
import { SemanticsScreen } from "./screens/SemanticsScreen";
import { BusinessMeaningScreen } from "./screens/BusinessMeaningScreen";
import { QualityScreen } from "./screens/QualityScreen";
import { AuditLedgerScreen } from "./screens/AuditLedgerScreen";
import { SourcesScreen } from "./screens/SourcesScreen";
import { OperationsScreen } from "./screens/OperationsScreen";
import { PersonaNav } from "./components/PersonaNav";
import { OrgPicker } from "./components/OrgPicker";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { fetchMe } from "./lib/api";
import type { MeRead } from "./lib/types";
import { asIdentityProvider, asPersona } from "./lib/ui-types";
import type { Persona } from "./lib/ui-types";
import "./App.css";

/* ---------------------------------------------------------------------------
   The shell.

   Module 21 §5: in production the persona is DERIVED from OIDC group claims,
   via `GET /v1/me` (persona_api.py), which itself derives it from
   `oidc.context_from_claims` -- the same configurable claim-path mechanism
   module 01 already uses for role mapping, extended to groups. The switcher
   in `PersonaNav` is a development convenience, gated by the identity
   provider `/v1/me` reports (module 01's existing prod-vs-dev-identity
   switch): it renders only under the development identity provider, and
   never under OIDC, where persona is not a user-selectable value. See
   `PersonaNav.tsx` for the enforcement point itself.

   Nav marked "legacy" still renders from ui/ (the existing portal). This is
   the strangle seam: the new shell owns routing and chrome from day one, and
   each screen moves across on its own schedule without a cutover.
--------------------------------------------------------------------------- */

const NAV: { id: string; label: string; group: string; ready?: boolean }[] = [
  { id: "home", label: "Get started", group: "Work", ready: true },
  { id: "analyst", label: "Ask", group: "Work", ready: true },
  { id: "catalog", label: "Catalog", group: "Discover", ready: true },
  { id: "marketplace", label: "Marketplace", group: "Discover", ready: true },
  { id: "relationships", label: "Relationships", group: "Discover", ready: true },
  { id: "lineage", label: "Lineage", group: "Understand", ready: true },
  { id: "semantics", label: "Semantics", group: "Understand", ready: true },
  { id: "meaning", label: "Business meaning", group: "Understand", ready: true },
  { id: "governance", label: "Review queue", group: "Govern", ready: true },
  { id: "studio", label: "Studio change sets", group: "Govern", ready: true },
  { id: "quality", label: "Quality", group: "Govern", ready: true },
  { id: "refusals", label: "Lineage refusals", group: "Govern", ready: true },
  { id: "audit", label: "Audit ledger", group: "Govern", ready: true },
  { id: "sources", label: "Sources", group: "Operate", ready: true },
  { id: "operations", label: "Operations", group: "Operate", ready: true },
];

export default function App() {
  const [view, setView] = useState("home");
  const [me, setMe] = useState<MeRead | null>(null);
  // The dev switcher's own choice. Irrelevant, and never read, once `me` reports
  // OIDC -- PersonaNav ignores `onPersonaChange` in that mode -- but kept as
  // state (rather than derived) so a dev's selection survives re-renders.
  const [devPersona, setDevPersona] = useState<Persona>("Steward");

  useEffect(() => {
    const controller = new AbortController();
    fetchMe(controller.signal)
      .then(setMe)
      .catch(() => {
        // Fail closed (module 01 INV-4): an unreachable /v1/me leaves `me` null,
        // which PersonaNav renders as nothing rather than guessing a mode.
      });
    return () => controller.abort();
  }, []);

  const identityProvider = asIdentityProvider(me?.identity_provider);
  const persona = identityProvider === "OIDC" ? asPersona(me?.persona) : devPersona;

  const groups = [...new Set(NAV.map((n) => n.group))];
  const current = NAV.find((n) => n.id === view);

  return (
    <div className="shell">
      <nav className="snav" aria-label="Main">
        <div className="snav__brand">
          <span className="snav__mark" aria-hidden="true" />
          <span className="snav__name">Atlas</span>
        </div>

        <OrgPicker />

        <PersonaNav
          identityProvider={identityProvider}
          persona={persona}
          onPersonaChange={setDevPersona}
        />

        {groups.map((g) => (
          <div key={g} className="snav__group">
            <div className="snav__ghead">{g}</div>
            {NAV.filter((n) => n.group === g).map((n) => (
              <button
                key={n.id}
                className="snav__item"
                data-nav={n.id}
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
        {view === "home" ? (
          <OnboardingWizard persona={persona} onNavigate={setView} />
        ) : view === "catalog" ? (
          <CatalogScreen />
        ) : view === "governance" ? (
          <ReviewQueueScreen />
        ) : view === "marketplace" ? (
          <MarketplaceScreen />
        ) : view === "refusals" ? (
          <LineageRefusalScreen />
        ) : view === "studio" ? (
          <StudioChangeSetsScreen />
        ) : view === "lineage" ? (
          <NarratedLineageScreen />
        ) : view === "analyst" ? (
          <AskScreen />
        ) : view === "relationships" ? (
          <RelationshipsScreen />
        ) : view === "semantics" ? (
          <SemanticsScreen />
        ) : view === "meaning" ? (
          <BusinessMeaningScreen />
        ) : view === "quality" ? (
          <QualityScreen />
        ) : view === "audit" ? (
          <AuditLedgerScreen />
        ) : view === "sources" ? (
          <SourcesScreen />
        ) : view === "operations" ? (
          <OperationsScreen />
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
