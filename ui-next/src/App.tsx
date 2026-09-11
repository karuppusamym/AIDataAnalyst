import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { HomeScreen } from "./screens/HomeScreen";
import { AgentInboxScreen } from "./screens/AgentInboxScreen";
import { PersonaNav } from "./components/PersonaNav";
import { Dialog } from "./components/primitives";
import { RouteErrorBoundary } from "./components/RouteErrorBoundary";
import { ScopePicker } from "./components/ScopePicker";
import { fetchMe } from "./lib/api";
import { APP_CONFIG } from "./lib/appConfig";
import {
  authBlock,
  authRevision,
  canSignOut,
  noteSignInFailure,
  subscribeAuth,
  type AuthBlock,
} from "./lib/authSession";
import { beginSignIn, canStartSignIn, signOut } from "./lib/oidcClient";
import { OrgProvider } from "./lib/org";
import { ScopeProvider } from "./lib/scope";
import { pushLocation, replaceLocation } from "./lib/location";
import { SCREEN_IDS, type ScreenId } from "./lib/routes";
import { describeSession, SessionProvider, useSession } from "./lib/session";
import { useCurrentScreen } from "./lib/useUrlState";
import { asIdentityProvider, asPersona } from "./lib/ui-types";
import type { Persona } from "./lib/ui-types";
import { landingWorkArea, WORK_AREAS, type WorkArea } from "./lib/workAreas";
import "./App.css";

const CatalogScreen = lazy(() => import("./screens/CatalogScreen").then((module) => ({ default: module.CatalogScreen })));
const DescriptionDraftsScreen = lazy(() => import("./screens/DescriptionDraftsScreen").then((module) => ({ default: module.DescriptionDraftsScreen })));
const DataDictionariesScreen = lazy(() => import("./screens/DataDictionariesScreen").then((module) => ({ default: module.DataDictionariesScreen })));
const ReviewQueueScreen = lazy(() => import("./screens/ReviewQueueScreen").then((module) => ({ default: module.ReviewQueueScreen })));
const ParsedLineageReviewScreen = lazy(() => import("./screens/ParsedLineageReviewScreen").then((module) => ({ default: module.ParsedLineageReviewScreen })));
const MarketplaceScreen = lazy(() => import("./screens/MarketplaceScreen").then((module) => ({ default: module.MarketplaceScreen })));
const LineageRefusalScreen = lazy(() => import("./screens/LineageRefusalScreen").then((module) => ({ default: module.LineageRefusalScreen })));
const StudioChangeSetsScreen = lazy(() => import("./screens/StudioChangeSetsScreen").then((module) => ({ default: module.StudioChangeSetsScreen })));
const NarratedLineageScreen = lazy(() => import("./screens/NarratedLineageScreen").then((module) => ({ default: module.NarratedLineageScreen })));
const CrossSourceScreen = lazy(() => import("./screens/CrossSourceScreen").then((module) => ({ default: module.CrossSourceScreen })));
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
const AgentGatewayScreen = lazy(() => import("./screens/AgentGatewayScreen").then((module) => ({ default: module.AgentGatewayScreen })));
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
const AgentRosterScreen = lazy(() => import("./screens/AgentRosterScreen").then((module) => ({ default: module.AgentRosterScreen })));
const ReviewerAgentScreen = lazy(() => import("./screens/ReviewerAgentScreen").then((module) => ({ default: module.ReviewerAgentScreen })));
const PlaybooksScreen = lazy(() => import("./screens/PlaybooksScreen").then((module) => ({ default: module.PlaybooksScreen })));
const DelegationsScreen = lazy(() => import("./screens/DelegationsScreen").then((module) => ({ default: module.DelegationsScreen })));
const PortfolioAnalyticsScreen = lazy(() => import("./screens/PortfolioAnalyticsScreen").then((module) => ({ default: module.PortfolioAnalyticsScreen })));
const NegativeKnowledgeScreen = lazy(() => import("./screens/NegativeKnowledgeScreen").then((module) => ({ default: module.NegativeKnowledgeScreen })));
const DocumentationWorklistScreen = lazy(() => import("./screens/DocumentationWorklistScreen").then((module) => ({ default: module.DocumentationWorklistScreen })));
const StewardAgentScreen = lazy(() => import("./screens/StewardAgentScreen").then((module) => ({ default: module.StewardAgentScreen })));
const LineageAgentScreen = lazy(() => import("./screens/LineageAgentScreen").then((module) => ({ default: module.LineageAgentScreen })));
const QualityAgentScreen = lazy(() => import("./screens/QualityAgentScreen").then((module) => ({ default: module.QualityAgentScreen })));

/* UX-20: navigation is organised by *work area*, not by feature area. Thirty
   flat items grouped by what the code does is a feature map; a person opening
   Atlas has a job to do, and the eight groups below are those jobs.

   F22: the groups are work areas, NOT personas. They used to share the word,
   which is why "Consumer" appeared here and in the persona landing map while
   being absent from the `Persona` union the switcher and `GET /v1/me` use --
   an entry nobody could ever select. The two vocabularies are now separate
   (`lib/workAreas.ts` holds the distinction) and every area is reachable from
   the sidebar regardless of persona.

   F09: `id` is a `ScreenId`, so a typo here is a compile error rather than a
   nav button that silently renders nothing. */
type NavItem = {
  id: ScreenId;
  label: string;
  group: WorkArea;
  icon: string;
  keywords: string;
};

const NAV: NavItem[] = [
  // --- Inbox: the supervisor's front door ---------------------------------
  { id: "home", label: "Overview", group: "Inbox", icon: "⌂", keywords: "home dashboard get started" },
  { id: "inbox", label: "Agent inbox", group: "Inbox", icon: "⧉", keywords: "agents proposals waiting decisions auto-applied sampled kill switch supervise" },
  // --- Analyst: answer a question, and trust the answer -------------------
  { id: "analyst", label: "Ask Atlas", group: "Analyst", icon: "✦", keywords: "question query analyst ai" },
  { id: "catalog", label: "Catalog", group: "Analyst", icon: "▦", keywords: "assets tables columns definitions descriptions data search" },
  { id: "semantics", label: "Semantic layer", group: "Analyst", icon: "ƒ", keywords: "metrics models measures" },
  { id: "tools", label: "Tool registry", group: "Analyst", icon: "⛭", keywords: "sql tool version execute registry" },
  { id: "tool-plans", label: "Tool plans", group: "Analyst", icon: "⛓", keywords: "orchestration multi-step budget validate execute evidence" },
  { id: "lineage", label: "Lineage", group: "Analyst", icon: "↗", keywords: "impact upstream downstream narrated" },
  { id: "unified-lineage", label: "Unified lineage", group: "Analyst", icon: "⇄", keywords: "graph impact upstream downstream unified" },
  // --- Consumer: use what has been approved -------------------------------
  { id: "marketplace", label: "Marketplace", group: "Consumer", icon: "◇", keywords: "products access request" },
  { id: "portfolio-analytics", label: "Portfolio analytics", group: "Consumer", icon: "▨", keywords: "marketplace portfolio analytics trends lifecycle usage quality data products" },
  // --- Developer: the audience that consumes context rather than reads it --
  //
  //   Context products sit here deliberately. A Consumer browses the
  //   marketplace and requests access to a product; a Developer *packages*
  //   context and points an agent at it. Those are different jobs, and
  //   keeping them in one group is why the gateway had nowhere obvious to
  //   live. Screen ids are unchanged, so `#/context` still resolves.
  { id: "context", label: "Context products", group: "Developer", icon: "◫", keywords: "context compile mcp rest yaml osi odcs snowflake databricks bindings rollout" },
  { id: "developer", label: "Agent gateway", group: "Developer", icon: "⇄", keywords: "mcp agent external client claude cursor endpoint token tools prompts resources consumption connect" },
  // --- Steward: make the estate mean something ----------------------------
  { id: "stewardship", label: "Stewardship", group: "Steward", icon: "⚑", keywords: "bulk tag classify own certify unowned backlog route escalation" },
  { id: "worklist", label: "Documentation worklist", group: "Steward", icon: "☰", keywords: "worklist priority usage impact deficit at-5 sw-1 rank document next" },
  { id: "steward-agent", label: "Steward agent", group: "Steward", icon: "✧", keywords: "steward agent adr-0029 draft propose descriptions glossary links worklist autonomy tier kill switch acceptance" },
  { id: "lineage-agent", label: "Lineage agent", group: "Steward", icon: "⤳", keywords: "lineage agent adr-0029 view definitions parse propose edges parsed lineage review kill switch" },
  { id: "quality-agent", label: "Quality agent", group: "Steward", icon: "⊻", keywords: "quality agent adr-0029 rules row count floor null rate ceiling profiles propose t2 kill switch" },
  { id: "playbooks", label: "Playbooks", group: "Steward", icon: "⚡", keywords: "playbook scheduled bulk tag classify own certify automation at-1" },
  { id: "negative-knowledge", label: "Negative knowledge", group: "Steward", icon: "⊘", keywords: "negative knowledge rejected suppressed assertions ee.3 material change" },
  { id: "meaning", label: "Business meaning", group: "Steward", icon: "Aa", keywords: "glossary terms annotations" },
  { id: "description-drafts", label: "Description drafts", group: "Steward", icon: "✎", keywords: "asset description draft generate submit steward" },
  { id: "relationships", label: "Relationships", group: "Steward", icon: "⌁", keywords: "keys graph links" },
  { id: "cross-source", label: "Cross-source", group: "Steward", icon: "⧉", keywords: "cross source domain federate identity resolution same object grant boundary discover" },
  { id: "transformations", label: "Transformations", group: "Steward", icon: "▤", keywords: "dbt models sql transforms manifest" },
  { id: "quality", label: "Data quality", group: "Steward", icon: "◎", keywords: "incidents score checks" },
  { id: "studio", label: "Studio", group: "Steward", icon: "△", keywords: "change sets author" },
  // --- Reviewer: decide, with the evidence in one pane --------------------
  { id: "governance", label: "Review queue", group: "Reviewer", icon: "✓", keywords: "approve reject proposals" },
  { id: "parsed-lineage-review", label: "Parsed lineage review", group: "Reviewer", icon: "↯", keywords: "lineage parsed view procedure dbt openlineage proposed approve reject p1-05" },
  { id: "refusals", label: "Policy refusals", group: "Reviewer", icon: "!", keywords: "lineage blocked denied" },
  { id: "reviewer-agent", label: "Reviewer agent", group: "Reviewer", icon: "◈", keywords: "reviewer agent adr-0027 auto-decide suspend disagreement sample audit tier0 tier1" },
  // --- Operator: keep the estate and the AI running -----------------------
  { id: "sources", label: "Sources", group: "Operator", icon: "▱", keywords: "connectors databases health model workbook excel columns import export" },
  { id: "operations", label: "Operations", group: "Operator", icon: "↻", keywords: "runs jobs ingestion outbox" },
  { id: "agents", label: "AI governance", group: "Operator", icon: "⌬", keywords: "model routes agents evaluations runtime" },
  { id: "ai", label: "AI registry", group: "Operator", icon: "◆", keywords: "agents models tools" },
  { id: "agent-roster", label: "Agent roster", group: "Operator", icon: "▥", keywords: "agent roster purpose method tool-first confidence auto-apply inspect" },
  { id: "access-policies", label: "Access policies", group: "Operator", icon: "⚖", keywords: "abac policy authorization simulation mask deny allow filter" },
  { id: "workspace-access", label: "Workspace access", group: "Operator", icon: "⚿", keywords: "members roles bindings approve reject bi tableau lineage connections" },
  { id: "delegations", label: "Delegations", group: "Operator", icon: "⇌", keywords: "delegation grant revoke governance authority pg-4 time-bounded" },
  { id: "reliability", label: "Reliability", group: "Operator", icon: "⏱", keywords: "slo error budget notification escalation archive worm audit archive data contract sla violations" },
  { id: "administration", label: "Administration", group: "Operator", icon: "⚙", keywords: "organization project datasource setup" },
  // --- Auditor: see everything, change nothing ----------------------------
  { id: "audit", label: "Audit ledger", group: "Auditor", icon: "≣", keywords: "events evidence history" },
  { id: "compliance", label: "Compliance packs", group: "Auditor", icon: "▣", keywords: "evidence audit framework generate download checksum" },
];

const NAV_BY_ID = new Map<ScreenId, NavItem>(NAV.map((item) => [item.id, item]));

/* Every routable screen must be reachable from the sidebar. A screen in the
 * route table with no nav entry is a page you can only get to by typing its
 * URL, which is how `portfolio-analytics` ended up filed under a comment for
 * a different group. Loud in development, inert in production -- a missing
 * nav row must not blank the app. */
if (import.meta.env?.DEV) {
  const missing = SCREEN_IDS.filter((id) => !NAV_BY_ID.has(id));
  if (missing.length) {
    console.warn(`App: screens with no navigation entry: ${missing.join(", ")}`);
  }
}

const GROUPS: readonly WorkArea[] = WORK_AREAS;

function Screen({
  view,
  personaKey,
  onNavigate,
}: {
  view: ScreenId;
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
    case "reviewer-agent": return <ReviewerAgentScreen />;
    case "studio": return <StudioChangeSetsScreen />;
    case "lineage": return <NarratedLineageScreen />;
    case "analyst": return <AskScreen />;
    case "relationships": return <RelationshipsScreen />;
    case "cross-source": return <CrossSourceScreen />;
    case "semantics": return <SemanticsScreen />;
    case "meaning": return <BusinessMeaningScreen />;
    case "quality": return <QualityScreen />;
    case "ai": return <AiRegistryScreen />;
    case "agent-roster": return <AgentRosterScreen />;
    case "audit": return <AuditLedgerScreen />;
    case "sources": return <SourcesScreen />;
    case "operations": return <OperationsScreen />;
    case "context": return <ContextProductsScreen />;
    case "developer": return <AgentGatewayScreen />;
    case "portfolio-analytics": return <PortfolioAnalyticsScreen />;
    case "tools": return <ToolRegistryScreen />;
    case "unified-lineage": return <UnifiedLineageScreen />;
    case "transformations": return <TransformationsScreen />;
    case "agents": return <AiGovernanceScreen />;
    case "administration": return <AdministrationScreen />;
    case "stewardship": return <StewardshipScreen />;
    case "worklist": return <DocumentationWorklistScreen />;
    case "steward-agent": return <StewardAgentScreen />;
    case "lineage-agent": return <LineageAgentScreen />;
    case "quality-agent": return <QualityAgentScreen />;
    case "playbooks": return <PlaybooksScreen />;
    case "negative-knowledge": return <NegativeKnowledgeScreen />;
    case "access-policies": return <AccessPolicyScreen />;
    case "compliance": return <ComplianceScreen />;
    case "tool-plans": return <ToolPlansScreen />;
    case "workspace-access": return <WorkspaceAccessScreen />;
    case "delegations": return <DelegationsScreen />;
    case "reliability": return <ReliabilityScreen />;
    case "inbox":
      return <AgentInboxScreen persona={personaKey} onNavigate={onNavigate} />;
    default: return null;
  }
}

/* ---------------------------------------------------------------------------
   The shell status badge (review 2026-09-05, F13 · T10).

   THE DEFECT it removes: "Platform connected" in the sidebar footer and "Live"
   in the top bar were literal strings in the JSX. They said the same thing
   with a backend down, a token expired, a tenant forbidden, or no backend
   configured at all -- and the `/me` error that would have contradicted them
   was discarded. The one indicator a user consults to decide whether to trust
   the screen was the one thing on the screen that could not be wrong, because
   it was not derived from anything.

   Everything below now comes from `describeSession`, which reads real request
   outcomes. Demo builds get their own tone, deliberately not the success one:
   fixture data must never be mistaken for a healthy connection.
--------------------------------------------------------------------------- */
function StatusBadge({ compact = false }: { compact?: boolean }) {
  const session = useSession();
  const described = describeSession(session);
  /* Only these three can be acted on from here. "Forbidden" is an answer
   * about this account in this organization -- retrying it changes nothing,
   * and offering a button that cannot help is how a shell teaches people to
   * ignore its buttons. */
  const recoverable =
    session.state === "degraded" ||
    session.state === "disconnected" ||
    session.state === "session-expired";

  return (
    <span
      className={`shellstatus${compact ? " shellstatus--compact" : ""}`}
      data-state={session.state}
      data-tone={described.tone}
      data-testid={compact ? "shell-status-compact" : "shell-status"}
    >
      <i className="shellstatus__dot" aria-hidden="true" />
      <span className="shellstatus__label">{described.label}</span>
      {compact ? (
        // The hint is the same sentence either way; in the compact footer it
        // is a tooltip rather than a second line of text.
        <span className="shellstatus__sr" title={described.hint}>
          <span className="visually-hidden">{described.hint}</span>
        </span>
      ) : (
        <span className="shellstatus__hint">{described.hint}</span>
      )}
      {!compact && recoverable ? (
        <button
          type="button"
          className="shellstatus__action"
          onClick={() => {
            /* An expired OIDC session cannot be recovered by repeating the
             * request that failed -- there is no token to send. Re-running the
             * authorization-code flow is the only thing that can help, so that
             * is what the button does when a flow exists. Everywhere else it
             * retries, which is all it ever could do. */
            if (session.state === "session-expired" && canStartSignIn()) {
              void beginSignIn().catch((cause: unknown) => {
                noteSignInFailure(cause instanceof Error ? cause.message : String(cause));
              });
              return;
            }
            session.reload();
          }}
          data-testid="session-reconnect"
        >
          {session.state === "session-expired" ? "Sign in again" : "Reconnect"}
        </button>
      ) : null}
      {!compact && canSignOut() ? (
        <button
          type="button"
          className="shellstatus__action"
          onClick={() => {
            // Through the flow module, not `clearAccessToken` directly: the
            // renewal timer and the refresh token are part of the session and
            // a sign-out that left either behind would quietly sign the user
            // back in a minute later.
            signOut();
            session.reload();
          }}
          data-testid="session-sign-out"
        >
          Sign out
        </button>
      ) : null}
    </span>
  );
}

/* F06/T07: a build configured for OIDC decides, before the first request,
   whether it can authenticate at all -- and it must say so once, here, rather
   than let forty screens each render their own 401 as an empty estate or a
   permissions problem.

   TWO SCREENS, ONE COMPONENT, because they are two answers to one question.
   With an issuer and a client id this is a sign-in screen and the button
   starts the real authorization-code + PKCE redirect. Without them there is
   nothing a user can do, and the screen says which build-time values are
   missing instead of offering a button that would fail. Neither version ever
   falls back to the development principal. */
function AuthBlockedScreen({ block }: { block: AuthBlock }) {
  const [starting, setStarting] = useState(false);
  const start = useCallback(() => {
    setStarting(true);
    // `beginSignIn` replaces the document on success, so nothing after this
    // runs in the happy path; the catch is for a discovery or crypto failure,
    // which has to be shown rather than leaving a spinner on screen forever.
    void beginSignIn().catch((cause: unknown) => {
      setStarting(false);
      noteSignInFailure(cause instanceof Error ? cause.message : String(cause));
    });
  }, []);

  return (
    /* `alert` only when the screen is reporting a problem. A sign-in prompt is
       not an alert, and announcing it as one trains people to ignore the role
       on the screens where it means something. */
    <div
      className="authblock"
      role={block.canSignIn ? "region" : "alert"}
      aria-label={block.canSignIn ? "Sign in" : undefined}
      data-testid="auth-blocked"
    >
      <div className="authblock__card">
        <h1 className="authblock__title">{block.title}</h1>
        <p className="authblock__detail">{block.detail}</p>
        <p className="authblock__remedy">{block.remedy}</p>
        {block.failure ? (
          <p className="authblock__failure" data-testid="auth-failure">
            Last attempt: {block.failure}
          </p>
        ) : null}
        {block.canSignIn ? (
          <button
            type="button"
            className="authblock__signin"
            onClick={start}
            disabled={starting}
            data-testid="auth-sign-in"
          >
            {starting ? "Redirecting…" : "Sign in"}
          </button>
        ) : null}
        <dl className="authblock__facts">
          <div>
            <dt>Data mode</dt>
            <dd>{APP_CONFIG.dataMode}</dd>
          </div>
          <div>
            <dt>Auth mode</dt>
            <dd>
              {APP_CONFIG.authMode}
              {APP_CONFIG.authModeInferred ? " (inferred)" : ""}
            </dd>
          </div>
          <div>
            <dt>Issuer</dt>
            <dd>{APP_CONFIG.oidc?.issuer ?? "not configured"}</dd>
          </div>
        </dl>
      </div>
    </div>
  );
}

function AppShell() {
  const view = useCurrentScreen();
  const session = useSession();
  const [devPersona, setDevPersona] = useState<Persona>("Steward");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const paletteInputRef = useRef<HTMLInputElement>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expandedGroup, setExpandedGroup] = useState<WorkArea | null>(
    () => NAV_BY_ID.get(view)?.group ?? GROUPS[0]!,
  );
  useEffect(() => {
    setExpandedGroup(NAV_BY_ID.get(view)?.group ?? null);
  }, [view]);

  /* F06/T07: when the token changes -- adopted, renewed, or lapsed -- ask the
   * backend again.
   *
   * THE DEFECT this removes, found by watching a real token expire: the badge
   * reported "Connected" over a token that had already lapsed, because the
   * connection state is derived from request OUTCOMES and no request had been
   * made since. Made-up green is the exact thing F13 removed from this badge;
   * a session that has ended is not allowed to reintroduce it. Re-running the
   * identity request produces the evidence -- a real 401 -- from which
   * `session.tsx` reports "Sign-in required". */
  const reloadSession = session.reload;
  useEffect(() => subscribeAuth(() => reloadSession()), [reloadSession]);

  const me = session.me;
  const identityProvider = asIdentityProvider(me?.identity_provider);
  const persona = identityProvider === "OIDC" ? asPersona(me?.persona) : devPersona;
  const personaKey = String(persona).toUpperCase();

  useEffect(() => {
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
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // UX-20: with no explicit route, land in the persona's own work area rather
  // than always on Overview. Runs once, and only when the URL names no screen
  // -- a deep link always wins over the default.
  const [landed, setLanded] = useState(false);
  useEffect(() => {
    if (landed || !me) return;
    setLanded(true);
    if (location.hash.replace(/^#\/?/, "") !== "") return;
    const area = landingWorkArea(persona);
    const first = area ? NAV.find((item) => item.group === area) : undefined;
    // Replace, not push: landing is not a navigation the user made, and Back
    // should leave the app rather than return to a URL naming no screen.
    if (first) replaceLocation({ screen: first.id });
  }, [landed, me, persona]);

  const current = NAV_BY_ID.get(view) ?? NAV[0]!;
  const sectionItems = NAV.filter((item) => item.group === current.group);

  /* F09: navigation goes through the one location store, which writes the
   * screen hash and keeps only the fields the TARGET screen declares.
   *
   * The old implementation defaulted the query to `location.search`, so the
   * filters of the page you were leaving arrived on the page you were opening
   * -- where a same-named field (`status`, `type`) meant something else
   * entirely. Dropping them is the fix, not a regression: estate context
   * (`ds`/`project`/`dom`) is still inherited by screens that declare it. */
  const navigate = (id: string, params?: Record<string, string>) => {
    setPaletteOpen(false);
    setNavOpen(false);
    const target = SCREEN_IDS.find((screen) => screen === id);
    if (!target) {
      if (import.meta.env?.DEV) console.warn(`App.navigate: unknown screen "${id}"`);
      return;
    }
    pushLocation({ screen: target, params });
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
              <button className="snav__ghead" aria-expanded={expandedGroup === group} aria-controls={`nav-${group}`} onClick={() => setExpandedGroup(expandedGroup === group ? null : group)}><span>{group}</span><span aria-hidden="true">{expandedGroup === group ? "-" : "+"}</span></button>
              <div id={`nav-${group}`} hidden={expandedGroup !== group}>
              {NAV.filter((item) => item.group === group).map((item) => (
                <button key={item.id} className="snav__item" data-nav={item.id} aria-current={item.id === view ? "page" : undefined} onClick={() => navigate(item.id)}>
                  <span className="snav__icon" aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              ))}
              </div>
            </div>
          ))}
        </div>

        <div className="snav__footer">
          <span className="snav__avatar" aria-hidden="true">{persona?.slice(0, 1) ?? "U"}</span>
          <span className="snav__who">
            <b>{persona ?? "Workspace user"}</b>
            <StatusBadge compact />
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
            {/* The visible label is `display:none` below a breakpoint, and the
                icon is aria-hidden, so without this the button announces as
                just "button" at narrow widths — axe-core `button-name` on
                every screen. The name is spelled out rather than left to the
                label, which is exactly the element that disappears. */}
            <button
              className="quickfind"
              aria-label="Jump to a page"
              onClick={() => setPaletteOpen(true)}
            >
              <span aria-hidden="true">⌕</span>
              <span className="quickfind__label">Jump to…</span>
              <kbd>Ctrl K</kbd>
            </button>
            <StatusBadge />
          </div>
        </header>

        {APP_CONFIG.authModeInferred && APP_CONFIG.dataMode === "live" ? (
          <p className="shellnotice" role="status" data-testid="auth-mode-inferred">
            No auth mode configured for this build. Requests are being made with the development
            identity, which a production backend will reject. Set <code>VITE_AUTH_MODE</code>.
          </p>
        ) : null}

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

        {/* F21: the lazy route lives inside an error boundary, so a chunk that
            fails to download costs this screen and not the whole app. The
            boundary resets on the screen id, which is what lets the user
            navigate away from a broken route without reloading. */}
        <div className="sview" key={view} data-screen={view}>
          <RouteErrorBoundary resetKey={view} label={current.label}>
            <Suspense fallback={<div className="screenloading" role="status">Loading {current.label}…</div>}>
              {view === "home" ? <HomeScreen persona={persona} onNavigate={navigate} /> : <Screen view={view} personaKey={personaKey} onNavigate={navigate} />}
            </Suspense>
          </RouteErrorBoundary>
        </div>
      </main>

      {/* F21: this palette was the review's own example of ARIA standing in for
        * interaction -- it declared `role="dialog" aria-modal="true"` with an
        * autofocused input and an Escape handler, and had no focus containment,
        * no focus restoration and no background inertness. Driving the running
        * app in a real browser confirmed it: 52 Tab presses walked out of the
        * open modal and into the sidebar navigation behind it. jsdom could not
        * show that, because jsdom has no sequential focus navigation.
        *
        * `Dialog` owns all three properties, so the palette states its content
        * and nothing else. */}
      {paletteOpen ? (
        <Dialog
          title="Quick navigation"
          description="Type to filter · choose a page to open it"
          onClose={() => setPaletteOpen(false)}
          initialFocusRef={paletteInputRef}
          className="palette__dialog"
        >
          <div className="palette__search">
            <span aria-hidden="true">⌕</span>
            <input
              ref={paletteInputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search pages and tools…"
              aria-label="Search pages"
            />
            <kbd>Esc</kbd>
          </div>
          <div className="palette__results">
            {matches.length ? (
              matches.map((item) => (
                <button key={item.id} className="palette__item" onClick={() => navigate(item.id)}>
                  <span className="snav__icon" aria-hidden="true">{item.icon}</span>
                  <span>
                    <b>{item.label}</b>
                    <small>{item.group}</small>
                  </span>
                  <span className="palette__arrow" aria-hidden="true">→</span>
                </button>
              ))
            ) : (
              <div className="palette__empty">No matching page</div>
            )}
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

/* The auth verdict is external state -- `authSession` owns it and notifies on
 * sign-in, renewal, lapse and sign-out -- so it is read the way React reads
 * external state. Recomputing `authBlock()` on every notification is cheap and
 * removes the need for a second copy of the answer in component state, which
 * is how the URL and the screen drifted apart in F09. */
function useAuthBlock(): AuthBlock | null {
  const revision = useSyncExternalStore(subscribeAuth, authRevision, authRevision);
  // `authBlock()` builds a fresh object per call, so the *revision* is what is
  // subscribed to and the verdict is derived from it. Handing a new object to
  // `useSyncExternalStore` as its snapshot would re-render forever.
  return useMemo(() => authBlock(), [revision]);
}

export default function App() {
  /* The blocked state is a property of the build's configuration plus whether
   * a token has ever been obtained, so it replaces the shell rather than
   * decorating it. Everything else -- expired, forbidden, degraded -- is a
   * request outcome and belongs in the badge inside the shell.
   *
   * F06: the estate providers live BELOW this gate. They used to wrap `App`
   * from `main.tsx`, so a build that could not authenticate still fired
   * `fetchOrganizations` on load -- a guaranteed 401 whose failure the user
   * never saw -- and, worse, that request ran exactly once: after signing in,
   * the organization list was still the empty one fetched before there was a
   * token, and every screen queried a tenant that was never resolved. Mounting
   * them after sign-in makes "signed in" the point at which the app starts
   * asking questions. */
  const block = useAuthBlock();
  if (block) return <AuthBlockedScreen block={block} />;

  return (
    <OrgProvider>
      <ScopeProvider>
        <SessionProvider fetchMe={fetchMe}>
          <AppShell />
        </SessionProvider>
      </ScopeProvider>
    </OrgProvider>
  );
}
