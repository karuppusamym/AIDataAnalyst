import { useMemo, useState } from "react";
import type { ContextProductRead, ProjectRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import {
  fetchContextProducts,
  fetchOrgProjects,
  requestContextProductDeprecation,
  submitContextProductVersion,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import {
  LoadingPanel,
  StatusStrip,
  useAsyncResource,
  useStatusChannel,
  useVersionLifecycle,
} from "../components/screenState";
import { CompilerPanel, useCompiler } from "./ContextProductCompiler";
import { NO_ROLLOUT, RolloutPanel, useRollout } from "./ContextProductRollout";
import { CreateDraftPanel } from "./ContextProductDraft";
import "./ContextProductsScreen.css";

/* ---------------------------------------------------------------------------
   Context products — the legacy portal's `context-products` view
   (`ui/index.html#context-products-view`,
   `ui/scripts/features/context-lineage-control-plane.js`), ported onto the
   real, already-merged `context_product_api.py` / `context_compiler_api.py`
   routes that view calls (see `lib/api.ts`'s "Context products" block for
   the exact endpoint list and what was deliberately left out).

   Four pieces, each in its own file, split along the lines the screen already
   described:
     1. registry      one row per product at its latest version (project-
                      scoped, same `fetchOrgProjects` picker `SemanticsScreen`
                      uses — `list_context_products` takes a `project_id`,
                      there is no org-wide browse), with Submit / Request
                      deprecation / Compile actions matching the version's
                      real lifecycle status. This file.
     2. compiler      `ContextProductCompiler.tsx` — target picker, the last
                      compiled artifact, and the download of it.
     3. rollout       `ContextProductRollout.tsx` — AT-7(b) consumer bindings.
     4. create draft  `ContextProductDraft.tsx` — every field
                      `ContextProductCreate` accepts, assembled only from
                      already-approved references.

   What stays here is the registry itself plus the one thing the four share:
   the single message strip. That mirrors the legacy screen's own single
   `#context-product-message` target — every action (load, create, submit,
   deprecate, compile, pin) reports through the same place a steward is
   already looking, rather than four panels each hiding their own outcome.
--------------------------------------------------------------------------- */

const versionStatusTone = (s: string): Tone =>
  s === "PUBLISHED" || s === "SUPPORTED"
    ? "ok"
    : s === "REVIEW_REQUIRED"
      ? "info"
      : s === "DEPRECATED" || s === "RETIRED"
        ? "bad"
        : "warn";

function ProductRow({
  product,
  busy,
  selected,
  onSubmit,
  onDeprecate,
  onCompile,
  onRollout,
}: {
  product: ContextProductRead;
  busy: string | null;
  selected: boolean;
  onSubmit: () => void;
  onDeprecate: () => void;
  onCompile: () => void;
  onRollout: () => void;
}) {
  const v = product.latest_version;
  const isBusy = busy === v.id;
  return (
    <article className={`cprow${selected ? " cprow--selected" : ""}`} aria-label={v.name}>
      <div className="cprow__main">
        <div className="cprow__badges">
          <Pill tone={versionStatusTone(v.status)}>{v.status.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <h3 className="cprow__title">{v.name}</h3>
        <div className="cprow__key">
          {product.product_key} · v{v.version}
        </div>
        <div className="cprow__grid">
          <div>
            <span className="cprow__label">Owner</span>
            <span>{v.owner_principal}</span>
          </div>
          <div>
            <span className="cprow__label">Consumers</span>
            <span>{v.allowed_consumer_roles.join(", ") || "—"}</span>
          </div>
          <div>
            <span className="cprow__label">Fingerprint</span>
            <code>{v.fingerprint.slice(0, 12)}</code>
          </div>
        </div>
      </div>
      {/* Which actions exist is decided by the version's real lifecycle
          status, never by a client-side guess: a DRAFT cannot be deprecated
          and a PUBLISHED version cannot be submitted again. */}
      <div className="cprow__actions">
        <Button onClick={onRollout} title="Pin named consumers to a specific version">
          {selected ? "Rollout ✓" : "Rollout"}
        </Button>
        {v.status === "DRAFT" ? (
          <Button disabled={isBusy} onClick={onSubmit}>
            {isBusy ? "Submitting…" : "Submit"}
          </Button>
        ) : null}
        {v.status === "PUBLISHED" || v.status === "SUPPORTED" ? (
          <Button disabled={isBusy} onClick={onDeprecate}>
            {isBusy ? "Requesting…" : "Deprecate"}
          </Button>
        ) : null}
        <Button variant="primary" disabled={isBusy} onClick={onCompile}>
          {isBusy ? "Compiling…" : "Compile"}
        </Button>
      </div>
    </article>
  );
}

export function ContextProductsScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");

  const channel = useStatusChannel();

  const projects = useAsyncResource<ProjectRead[]>(
    async (signal) => (await fetchOrgProjects(ORG, signal)).items,
    [ORG],
  );

  /* `enabled` is what makes the assertion safe: `list_context_products` takes
     a project id and there is no org-wide browse, so with no project there is
     nothing to request. The load reports what it found through the same strip
     every action uses -- which is why it supersedes a just-completed action's
     message, exactly as legacy's single `#context-product-message` did. */
  const registry = useAsyncResource<PageOf<ContextProductRead>>(
    (signal) => fetchContextProducts(projectId!, { limit: 200 }, signal),
    [projectId],
    {
      enabled: Boolean(projectId),
      onLoad: (page) =>
        channel.success(`${page.total} governed product${page.total === 1 ? "" : "s"} in this project.`),
    },
  );
  const reloadRegistry = registry.reload;
  const items = useMemo(() => registry.data?.items ?? [], [registry.data]);

  const lifecycle = useVersionLifecycle(channel, reloadRegistry);
  const compiler = useCompiler(channel);

  const [rolloutProduct, setRolloutProduct] = useState<ContextProductRead | null>(null);
  const rollout = useRollout(rolloutProduct, channel);
  const { versions, bindings } = rollout.resource.data ?? NO_ROLLOUT;

  /* Compiling blocks the row that started it, the same way a lifecycle
     request does, but it does not change the version's status -- so it is
     tracked by the compiler and not routed through `useVersionLifecycle`. */
  const busyVersionId = lifecycle.busyVersionId ?? compiler.busyVersionId;

  return (
    <div className="cpscreen">
      <header className="cpscreen__head">
        <div>
          <p className="cpscreen__eyebrow">GOVERNED AGENT CONTEXT</p>
          <h1 className="cpscreen__h1">Context products</h1>
          <p className="cpscreen__lede">
            Package exact metadata, semantics, terms, quality gates, and eligible tools into immutable versions.
          </p>
        </div>
        <div className="cpscreen__filters">
          <Field label="Project">
            <select value={projectId ?? ""} onChange={(e) => setParams({ project: e.target.value || null })}>
              <option value="">Select a project…</option>
              {(projects.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Button onClick={reloadRegistry}>Refresh</Button>
          {/* The package is only half the story: the gateway is where an
              agent developer learns how to actually connect to it. */}
          <Button onClick={() => navigateTo("developer", projectId ? { project: projectId } : {})}>
            Agent gateway →
          </Button>
        </div>
      </header>

      {projects.error ? (
        <p className="cpscreen__pickerr" role="alert">
          {projects.error}
        </p>
      ) : null}
      <StatusStrip status={channel.status} />

      <div className="cpscreen__body">
        <div className="cpscreen__main">
          <article className="cpregistry">
            <header className="cpregistry__head">
              <p className="cpregistry__eyebrow">VERSION REGISTRY</p>
              <h2 className="cpregistry__h2">Publication posture</h2>
              <p className="cpregistry__lede">
                Review lifecycle, consumers, and fingerprints before compiling or submitting any version.
              </p>
            </header>
            {!projectId ? (
              <Empty
                title="Pick a project to see its context products"
                hint="Context products are project-scoped, same as Semantics."
              />
            ) : registry.error ? (
              <ErrorState title="Context products could not be loaded" detail={registry.error} onRetry={reloadRegistry} />
            ) : registry.loading ? (
              <LoadingPanel label="Loading governed products…" />
            ) : (
              <VirtualList
                items={items}
                getKey={(p) => p.id}
                ariaLabel="Context products"
                estimateSize={132}
                totalCount={registry.data?.total ?? null}
                emptyState={
                  <Empty
                    title="No Context Products"
                    hint="Create a bounded product from approved tables, semantics, terms, and tools."
                  />
                }
                renderItem={(p) => (
                  <ProductRow
                    product={p}
                    busy={busyVersionId}
                    selected={rolloutProduct?.id === p.id}
                    onSubmit={() =>
                      void lifecycle.run(p.latest_version.id, {
                        pending: "Requesting publication review",
                        done: "Publication review requested.",
                        action: submitContextProductVersion,
                      })
                    }
                    onDeprecate={() =>
                      void lifecycle.run(p.latest_version.id, {
                        pending: "Requesting deprecation review",
                        done: "Deprecation review requested.",
                        action: requestContextProductDeprecation,
                      })
                    }
                    onCompile={() => void compiler.compile(p.latest_version.id)}
                    onRollout={() => setRolloutProduct((current) => (current?.id === p.id ? null : p))}
                  />
                )}
              />
            )}
          </article>

          {rolloutProduct ? (
            <RolloutPanel
              product={rolloutProduct}
              versions={versions}
              bindings={bindings}
              loading={rollout.resource.loading}
              error={rollout.resource.error}
              busyConsumer={rollout.busyConsumer}
              onBind={rollout.bind}
              onUnbind={rollout.unbind}
              onReload={rollout.resource.reload}
              onClose={() => setRolloutProduct(null)}
            />
          ) : null}

          <CompilerPanel
            target={compiler.target}
            onTargetChange={compiler.setTarget}
            result={compiler.result}
            compiling={compiler.compiling}
            onDownload={() => void compiler.download()}
            downloading={compiler.downloading}
          />
        </div>

        <aside className="cpscreen__rail">
          <CreateDraftPanel orgId={ORG} projectId={projectId} channel={channel} onCreated={reloadRegistry} />

          <article className="cpguide">
            <p className="cpguide__eyebrow">COMPOSITION GUIDE</p>
            <h2 className="cpguide__h2">What belongs here</h2>
            <div className="cpguide__list">
              <div>
                <strong>Approved references only</strong>
                <span>
                  Use reviewed table, semantic, glossary, and tool versions so the package can be compiled
                  deterministically.
                </span>
              </div>
              <div>
                <strong>Purpose before payload</strong>
                <span>State the exact consumer purpose first, then include only the context needed for that purpose.</span>
              </div>
              <div>
                <strong>Quality gates stay visible</strong>
                <span>Set lineage depth and minimum quality so consumers inherit operational guardrails with the package.</span>
              </div>
            </div>
          </article>
        </aside>
      </div>
    </div>
  );
}
