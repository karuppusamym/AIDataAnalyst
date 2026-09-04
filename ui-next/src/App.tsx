import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { HomeScreen } from "./screens/HomeScreen";
import { AgentInboxScreen } from "./screens/AgentInboxScreen";
import { PersonaNav } from "./components/PersonaNav";
import { ScopePicker } from "./components/ScopePicker";
import { fetchMe } from "./lib/api";
import type { MeRead } from "./lib/types";
import { asIdentityProvider, asPersona } from "./lib/ui-types";
import type { Persona } from "./lib/ui-types";
import "./App.css";

const CatalogScreen = lazy(() => import("./screens/CatalogScreen").then((module) => ({ default: module.CatalogScreen })));
const DescriptionDraftsScreen = lazy(() => import("./screens/DescriptionDraftsScreen").then((module) => ({ default: module.DescriptionDraftsScreen })));
const ReviewQueueScreen = lazy(() => import("./screens/ReviewQueueScreen").then((module) => ({ default: module.ReviewQueueScreen })));
const ParsedLineageReviewScreen = lazy(() => import("./screens/ParsedLineageReviewScreen").then((module) => ({ default: module.ParsedLineageReviewScreen })));
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

/* UX-20: navigation is organised by *persona workbench*, not by feature area.
   Thirty flat items grouped by what the code does is a feature map; a person
   opening Atlas is one of six personas with a job, and the six groups below
   are `Docs/00-product/02-personas-and-jobs.md`'s own list. Every screen id
   is unchanged, so every existing deep link (`#/catalog`, `#/governance`, …)
   still resolves -- only the grouping moved. */
type Workbench =
  | "Inbox"
  | "Analyst"
  | "Consumer"
  | "Steward"
  | "Reviewer"
  | "Operator"
  | "Auditor";

type NavItem = {
  id: string;
  label: string;
  group: Workbench;
  icon: string;
  keywords: string;
};

const NAV: NavItem[] = [
  // --- Inbox: the supervisor's front door ---------------------------------
  { id: "home", label: "Overview", group: "Inbox", icon: "⌂", keywords: "home dashboard get started" },
  { id: "inbox", label: "Agent inbox", group: "Inbox", icon: "⧉", keywords: "agents proposals waiting decisions auto-applied sampled kill switch supervise" },
  // --- Analyst: answer a question, and trust the answer -------------------
  { id: "analyst", label: "Ask Atlas", group: "Analyst", icon: "✦", keywords: "question query analyst ai" },
  { id: "catalog", label: "Catalog", group: "Analyst", icon: "▦", keywords: "assets tables data search" },
  { id: "semantics", label: "Semantic layer", group: "Analyst", icon: "ƒ", keywords: "metrics models measures" },
  { id: "tools", label: "Tool registry", group: "Analyst", icon: "⛭", keywords: "sql tool version execute registry" },
  { id: "tool-plans", label: "Tool plans", group: "Analyst", icon: "⛓", keywords: "orchestration multi-step budget validate execute evidence" },
  { id: "lineage", label: "Lineage", group: "Analyst", icon: "↗", keywords: "impact upstream downstream narrated" },
  { id: "unified-lineage", label: "Unified lineage", group: "Analyst", icon: "⇄", keywords: "graph impact upstream downstream unified" },
  // --- Consumer: use what has been approved -------------------------------
  { id: "marketplace", label: "Marketplace", group: "Consumer", icon: "◇", keywords: "products access request" },
  { id: "context", label: "Context products", group: "Consumer", icon: "◫", keywords: "context compile mcp rest" },
  // --- Steward: make the estate mean something ----------------------------
  { id: "stewardship", label: "Stewardship", group: "Steward", icon: "⚑", keywords: "bulk tag classify own certify unowned backlog route escalation" },
  { id: "meaning", label: "Business meaning", group: "Steward", icon: "Aa", keywords: "glossary terms annotations" },
  { id: "description-drafts", label: "Description drafts", group: "Steward", icon: "✎", keywords: "asset description draft generate submit steward" },
  { id: "relationships", label: "Relationships", group: "Steward", icon: "⌁", keywords: "keys graph links" },
  { id: "transformations", label: "Transformations", group: "Steward", icon: "▤", keywords: "dbt models sql transforms manifest" },
  { id: "quality", label: "Data quality", group: "Steward", icon: "◎", keywords: "incidents score checks" },
  { id: "studio", label: "Studio", group: "Steward", icon: "△", keywords: "change sets author" },
  // --- Reviewer: decide, with the evidence in one pane --------------------
  { id: "governance", label: "Review queue", group: "Reviewer", icon: "✓", keywords: "approve reject proposals" },
  { id: "parsed-lineage-review", label: "Parsed lineage review", group: "Reviewer", icon: "↯", keywords: "lineage parsed view procedure dbt openlineage proposed approve reject p1-05" },
  { id: "refusals", label: "Policy refusals", group: "Reviewer", icon: "!", keywords: "lineage blocked denied" },
  // --- Operator: keep the estate and the AI running -----------------------
  { id: "sources", label: "Sources", group: "Operator", icon: "▱", keywords: "connectors databases health" },
  { id: "operations", label: "Operations", group: "Operator", icon: "↻", keywords: "runs jobs ingestion outbox" },
  { id: "agents", label: "AI governance", group: "Operator", icon: "⌬", keywords: "model routes agents evaluations runtime" },
  { id: "ai", label: "AI registry", group: "Operator", icon: "◆", keywords: "agents models tools" },
  { id: "access-policies", label: "Access policies", group: "Operator", icon: "⚖", keywords: "abac policy authorization simulation mask deny allow filter" },
  { id: "workspace-access", label: "Workspace access", group: "Operator", icon: "⚿", keywords: "members roles bindings approve reject bi tableau lineage connections" },
  { id: "reliability", label: "Reliability", group: "Operator", icon: "⏱", keywords: "slo error budget notification escalation archive worm audit archive data contract sla violations" },
  { id: "administration", label: "Administration", group: "Operator", icon: "⚙", keywords: "organization project datasource setup" },
  // --- Auditor: see everything, change nothing ----------------------------
  { id: "audit", label: "Audit ledger", group: "Auditor", icon: "≣", keywords: "events evidence history" },
  { id: "compliance", label: "Compliance packs", group: "Auditor", icon: "▣", keywords: "evidence audit framework generate download checksum" },
];

const GROUPS: Workbench[] = [
  "Inbox",
  "Analyst",
  "Consumer",
  "Steward",
  "Reviewer",
  "Operator",
  "Auditor",
];

/** The workbench a persona lands in. Everyone can open every workbench they
 *  are entitled to -- this only chooses the first one. */
const WORKBENCH_BY_PERSONA: Record<string, Workbench> = {
  Analyst: "Analyst",
  Consumer: "Consumer",
  Steward: "Steward",
  Reviewer: "Reviewer",
  Operator: "Operator",
  Auditor: "Auditor",
};

function currentFromHash(): string {
  const candidate = location.hash.replace(/^#\/?/, "");
  return NAV.some((item) => item.id === candidate) ? candidate : "home";
}

function Screen({
  view,
  personaKey,
  onNavigate,
}: {
  view: string;
  personaKey: string;
  onNavigate: (view: string, params?: Record<string, string>) => void;
}) {
  switch (view) {
    case "catalog": return <CatalogScreen />;
    case "governance": return <ReviewQueueScreen />;
    case "parsed-lineage-review": return <ParsedLineageReviewScreen />;
    case "description-drafts": return <DescriptionDraftsScreen />;
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
    case "inbox":
      return <AgentInboxScreen persona={personaKey} onNavigate={onNavigate} />;
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
  const personaKey = String(persona).toUpperCase();

  // UX-20: with no explicit route, land in the persona's own workbench
  // rather than always on Overview. Runs once, and only when the URL names
  // no screen -- a deep link always wins over the default.
  const [landed, setLanded] = useState(false);
  useEffect(() => {
    if (landed || !me) return;
    setLanded(true);
    if (location.hash.replace(/^#\/?/, "") !== "") return;
    const workbench = WORKBENCH_BY_PERSONA[String(persona)];
    const first = workbench ? NAV.find((item) => item.group === workbench) : undefined;
    if (first) {
      setView(first.id);
      history.replaceState(null, "", `${location.pathname}${location.search}#/${first.id}`);
    }
  }, [landed, me, persona]);
  const current = NAV.find((item) => item.id === view) ?? NAV[0]!;
  const sectionItems = NAV.filter((item) => item.group === current.group);

  const navigate = (id: string, params?: Record<string, string>) => {
    setView(id);
    setPaletteOpen(false);
    setNavOpen(false);
    // Query params ride alongside the hash route so a screen the inbox opened
    // lands focused on the right row (the `useUrlState` convention every
    // migrated screen already reads).
    const query = params ? `?${new URLSearchParams(params).toString()}` : location.search;
    const target = `${location.pathname}${query}#/${id}`;
    if (`${location.search}${location.hash}` !== `${query}#/${id}`) {
      history.pushState(null, "", target);
    }
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
            {view === "home" ? <HomeScreen persona={persona} onNavigate={navigate} /> : <Screen view={view} personaKey={personaKey} onNavigate={navigate} />}
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
