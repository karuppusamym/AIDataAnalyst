import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { HomeScreen } from "./screens/HomeScreen";
import { PersonaNav } from "./components/PersonaNav";
import { ScopePicker } from "./components/ScopePicker";
import { fetchMe } from "./lib/api";
import type { MeRead } from "./lib/types";
import { asIdentityProvider, asPersona } from "./lib/ui-types";
import type { Persona } from "./lib/ui-types";
import "./App.css";

const CatalogScreen = lazy(() => import("./screens/CatalogScreen").then((module) => ({ default: module.CatalogScreen })));
const ReviewQueueScreen = lazy(() => import("./screens/ReviewQueueScreen").then((module) => ({ default: module.ReviewQueueScreen })));
const MarketplaceScreen = lazy(() => import("./screens/MarketplaceScreen").then((module) => ({ default: module.MarketplaceScreen })));
const LineageRefusalScreen = lazy(() => import("./screens/LineageRefusalScreen").then((module) => ({ default: module.LineageRefusalScreen })));
const StudioChangeSetsScreen = lazy(() => import("./screens/StudioChangeSetsScreen").then((module) => ({ default: module.StudioChangeSetsScreen })));
const NarratedLineageScreen = lazy(() => import("./screens/NarratedLineageScreen").then((module) => ({ default: module.NarratedLineageScreen })));
const AskScreen = lazy(() => import("./screens/AskScreen").then((module) => ({ default: module.AskScreen })));
const RelationshipsScreen = lazy(() => import("./screens/RelationshipsScreen").then((module) => ({ default: module.RelationshipsScreen })));
const SemanticsScreen = lazy(() => import("./screens/SemanticsScreen").then((module) => ({ default: module.SemanticsScreen })));
const BusinessMeaningScreen = lazy(() => import("./screens/BusinessMeaningScreen").then((module) => ({ default: module.BusinessMeaningScreen })));
const QualityScreen = lazy(() => import("./screens/QualityScreen").then((module) => ({ default: module.QualityScreen })));
const AuditLedgerScreen = lazy(() => import("./screens/AuditLedgerScreen").then((module) => ({ default: module.AuditLedgerScreen })));
const SourcesScreen = lazy(() => import("./screens/SourcesScreen").then((module) => ({ default: module.SourcesScreen })));
const OperationsScreen = lazy(() => import("./screens/OperationsScreen").then((module) => ({ default: module.OperationsScreen })));
const AiRegistryScreen = lazy(() => import("./screens/AiRegistryScreen").then((module) => ({ default: module.AiRegistryScreen })));
const ContextProductsScreen = lazy(() => import("./screens/ContextProductsScreen").then((module) => ({ default: module.ContextProductsScreen })));
const AdministrationScreen = lazy(() => import("./screens/AdministrationScreen").then((module) => ({ default: module.AdministrationScreen })));
const ToolRegistryScreen = lazy(() => import("./screens/ToolRegistryScreen").then((module) => ({ default: module.ToolRegistryScreen })));
const UnifiedLineageScreen = lazy(() => import("./screens/UnifiedLineageScreen").then((module) => ({ default: module.UnifiedLineageScreen })));
const AiGovernanceScreen = lazy(() => import("./screens/AiGovernanceScreen").then((module) => ({ default: module.AiGovernanceScreen })));
const TransformationsScreen = lazy(() => import("./screens/TransformationsScreen").then((module) => ({ default: module.TransformationsScreen })));
const StewardshipScreen = lazy(() => import("./screens/StewardshipScreen").then((module) => ({ default: module.StewardshipScreen })));
const WorkspaceAccessScreen = lazy(() => import("./screens/WorkspaceAccessScreen").then((module) => ({ default: module.WorkspaceAccessScreen })));
const AccessPolicyScreen = lazy(() => import("./screens/AccessPolicyScreen").then((module) => ({ default: module.AccessPolicyScreen })));
const ReliabilityScreen = lazy(() => import("./screens/ReliabilityScreen").then((module) => ({ default: module.ReliabilityScreen })));
const ComplianceScreen = lazy(() => import("./screens/ComplianceScreen").then((module) => ({ default: module.ComplianceScreen })));
const ToolPlansScreen = lazy(() => import("./screens/ToolPlansScreen").then((module) => ({ default: module.ToolPlansScreen })));

type NavItem = {
  id: string;
  label: string;
  group: "Work" | "Discover" | "Understand" | "Govern" | "Control" | "Operate";
  icon: string;
  keywords: string;
};

const NAV: NavItem[] = [
  { id: "home", label: "Overview", group: "Work", icon: "⌂", keywords: "home dashboard get started" },
  { id: "analyst", label: "Ask Atlas", group: "Work", icon: "✦", keywords: "question query analyst ai" },
  { id: "catalog", label: "Catalog", group: "Discover", icon: "▦", keywords: "assets tables data search" },
  { id: "marketplace", label: "Marketplace", group: "Discover", icon: "◇", keywords: "products access request" },
  { id: "relationships", label: "Relationships", group: "Discover", icon: "⌁", keywords: "keys graph links" },
  { id: "lineage", label: "Lineage", group: "Understand", icon: "↗", keywords: "impact upstream downstream narrated" },
  { id: "semantics", label: "Semantic layer", group: "Understand", icon: "ƒ", keywords: "metrics models measures" },
  { id: "meaning", label: "Business meaning", group: "Understand", icon: "Aa", keywords: "glossary terms annotations" },
  { id: "context", label: "Context products", group: "Understand", icon: "◫", keywords: "context compile mcp rest" },
  { id: "tools", label: "Tool registry", group: "Understand", icon: "⛭", keywords: "sql tool version execute registry" },
  { id: "unified-lineage", label: "Unified lineage", group: "Understand", icon: "⇄", keywords: "graph impact upstream downstream unified" },
  { id: "transformations", label: "Transformations", group: "Understand", icon: "▤", keywords: "dbt models sql transforms manifest" },
  { id: "agents", label: "AI governance", group: "Govern", icon: "⌬", keywords: "model routes agents evaluations runtime" },
  { id: "governance", label: "Review queue", group: "Govern", icon: "✓", keywords: "approve reject proposals" },
  { id: "studio", label: "Studio", group: "Govern", icon: "△", keywords: "change sets author" },
  { id: "quality", label: "Data quality", group: "Govern", icon: "◎", keywords: "incidents score checks" },
  { id: "ai", label: "AI registry", group: "Govern", icon: "◆", keywords: "agents models tools" },
  { id: "refusals", label: "Policy refusals", group: "Govern", icon: "!", keywords: "lineage blocked denied" },
  { id: "audit", label: "Audit ledger", group: "Govern", icon: "≣", keywords: "events evidence history" },
  { id: "stewardship", label: "Stewardship", group: "Control", icon: "⚑", keywords: "bulk tag classify own certify unowned backlog route escalation" },
  { id: "access-policies", label: "Access policies", group: "Control", icon: "⚖", keywords: "abac policy authorization simulation mask deny allow filter" },
  { id: "compliance", label: "Compliance packs", group: "Control", icon: "▣", keywords: "evidence audit framework generate download checksum" },
  { id: "tool-plans", label: "Tool plans", group: "Control", icon: "⛓", keywords: "orchestration multi-step budget validate execute evidence" },
  { id: "workspace-access", label: "Workspace access", group: "Control", icon: "⚿", keywords: "members roles bindings approve reject bi tableau lineage connections" },
  { id: "reliability", label: "Reliability", group: "Control", icon: "⏱", keywords: "slo error budget notification escalation archive worm audit archive data contract sla violations" },
  { id: "administration", label: "Administration", group: "Control", icon: "⚙", keywords: "organization project datasource setup" },
  { id: "sources", label: "Sources", group: "Operate", icon: "▱", keywords: "connectors databases health" },
  { id: "operations", label: "Operations", group: "Operate", icon: "↻", keywords: "runs jobs ingestion outbox" },
];

const GROUPS: NavItem["group"][] = ["Work", "Discover", "Understand", "Govern", "Control", "Operate"];

function currentFromHash(): string {
  const candidate = location.hash.replace(/^#\/?/, "");
  return NAV.some((item) => item.id === candidate) ? candidate : "home";
}

function Screen({ view }: { view: string }) {
  switch (view) {
    case "catalog": return <CatalogScreen />;
    case "governance": return <ReviewQueueScreen />;
    case "marketplace": return <MarketplaceScreen />;
    case "refusals": return <LineageRefusalScreen />;
    case "studio": return <StudioChangeSetsScreen />;
    case "lineage": return <NarratedLineageScreen />;
    case "analyst": return <AskScreen />;
    case "relationships": return <RelationshipsScreen />;
    case "semantics": return <SemanticsScreen />;
    case "meaning": return <BusinessMeaningScreen />;
    case "quality": return <QualityScreen />;
    case "ai": return <AiRegistryScreen />;
    case "audit": return <AuditLedgerScreen />;
    case "sources": return <SourcesScreen />;
    case "operations": return <OperationsScreen />;
    case "context": return <ContextProductsScreen />;
    case "tools": return <ToolRegistryScreen />;
    case "unified-lineage": return <UnifiedLineageScreen />;
    case "transformations": return <TransformationsScreen />;
    case "agents": return <AiGovernanceScreen />;
    case "administration": return <AdministrationScreen />;
    case "stewardship": return <StewardshipScreen />;
    case "access-policies": return <AccessPolicyScreen />;
    case "compliance": return <ComplianceScreen />;
    case "tool-plans": return <ToolPlansScreen />;
    case "workspace-access": return <WorkspaceAccessScreen />;
    case "reliability": return <ReliabilityScreen />;
    default: return null;
  }
}

export default function App() {
  const [view, setView] = useState(currentFromHash);
  const [me, setMe] = useState<MeRead | null>(null);
  const [devPersona, setDevPersona] = useState<Persona>("Steward");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetchMe(controller.signal).then(setMe).catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const onHashChange = () => setView(currentFromHash());
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setNavOpen(false);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    window.addEventListener("popstate", onHashChange);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("hashchange", onHashChange);
      window.removeEventListener("popstate", onHashChange);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const identityProvider = asIdentityProvider(me?.identity_provider);
  const persona = identityProvider === "OIDC" ? asPersona(me?.persona) : devPersona;
  const current = NAV.find((item) => item.id === view) ?? NAV[0]!;
  const sectionItems = NAV.filter((item) => item.group === current.group);

  const navigate = (id: string) => {
    setView(id);
    setPaletteOpen(false);
    setNavOpen(false);
    if (location.hash !== `#/${id}`) history.pushState(null, "", `#/${id}`);
  };

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return NAV;
    return NAV.filter((item) => `${item.label} ${item.group} ${item.keywords}`.toLowerCase().includes(needle));
  }, [query]);

  return (
    <div className={`shell${navOpen ? " shell--nav-open" : ""}`}>
      <nav className="snav" aria-label="Main">
        <div className="snav__brand">
          <span className="snav__mark" aria-hidden="true">A</span>
          <span>
            <span className="snav__name">Atlas</span>
            <span className="snav__edition">Data intelligence</span>
          </span>
          <button className="snav__mobile-close" onClick={() => setNavOpen(false)} aria-label="Close navigation">×</button>
        </div>

        <div className="snav__context">
          <ScopePicker />
          <PersonaNav identityProvider={identityProvider} persona={persona} onPersonaChange={setDevPersona} />
        </div>

        <div className="snav__links">
          {GROUPS.map((group) => (
            <div key={group} className="snav__group">
              <div className="snav__ghead">{group}</div>
              {NAV.filter((item) => item.group === group).map((item) => (
                <button key={item.id} className="snav__item" data-nav={item.id} aria-current={item.id === view ? "page" : undefined} onClick={() => navigate(item.id)}>
                  <span className="snav__icon" aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              ))}
            </div>
          ))}
        </div>

        <div className="snav__footer">
          <span className="snav__avatar" aria-hidden="true">{persona?.slice(0, 1) ?? "U"}</span>
          <span className="snav__who">
            <b>{persona ?? "Workspace user"}</b>
            <small><i aria-hidden="true" /> Platform connected</small>
          </span>
        </div>
      </nav>

      <button className="shell__scrim" onClick={() => setNavOpen(false)} aria-label="Close navigation" />

      <main className="smain">
        <header className="topbar">
          <div className="topbar__trail">
            <button className="topbar__menu" onClick={() => setNavOpen(true)} aria-label="Open navigation">☰</button>
            <span className="topbar__workspace">Workspace</span>
            <span className="topbar__slash" aria-hidden="true">/</span>
            <strong>{current.label}</strong>
          </div>
          <div className="topbar__actions">
            <button className="quickfind" onClick={() => setPaletteOpen(true)}>
              <span aria-hidden="true">⌕</span>
              <span className="quickfind__label">Jump to…</span>
              <kbd>Ctrl K</kbd>
            </button>
            <span className="topbar__status"><i aria-hidden="true" /> Live</span>
          </div>
        </header>

        <nav className="sectionnav" aria-label={`${current.group} pages`}>
          <span className="sectionnav__label">{current.group}</span>
          <div className="sectionnav__scroll">
            {sectionItems.map((item) => (
              <button
                key={item.id}
                className="sectionnav__item"
                aria-current={item.id === view ? "page" : undefined}
                onClick={() => navigate(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </nav>

        <div className="sview">
          <Suspense fallback={<div className="screenloading" role="status">Loading {current.label}…</div>}>
            {view === "home" ? <HomeScreen persona={persona} onNavigate={navigate} /> : <Screen view={view} />}
          </Suspense>
        </div>
      </main>

      {paletteOpen ? (
        <div className="palette" role="presentation" onMouseDown={() => setPaletteOpen(false)}>
          <section className="palette__dialog" role="dialog" aria-modal="true" aria-label="Quick navigation" onMouseDown={(event) => event.stopPropagation()}>
            <div className="palette__search">
              <span aria-hidden="true">⌕</span>
              <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search pages and tools…" aria-label="Search pages" />
              <kbd>Esc</kbd>
            </div>
            <div className="palette__results">
              {matches.length ? matches.map((item) => (
                <button key={item.id} className="palette__item" onClick={() => navigate(item.id)}>
                  <span className="snav__icon" aria-hidden="true">{item.icon}</span>
                  <span><b>{item.label}</b><small>{item.group}</small></span>
                  <span className="palette__arrow" aria-hidden="true">→</span>
                </button>
              )) : <div className="palette__empty">No matching page</div>}
            </div>
            <footer className="palette__foot">Type to filter · choose a page to open it</footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
