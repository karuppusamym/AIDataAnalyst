import { useCallback, useEffect, useMemo, useRef, useState } from "react";
/* Filters and selection live in the URL so a filtered view is shareable and
   survives Back/Forward. This screen carried a verbatim copy of the old hook
   -- a `useState` seeded once from `location.search`, subscribed to nothing --
   so its idea of the selection and the address bar drifted apart the first
   time either the Back button or a same-screen link was used (review
   2026-09-05, F09 - R07). The shared hook reads one location store. */
import { useUrlState } from "../lib/useUrlState";
import type { MarketplaceProductRead } from "../lib/ui-types";
import type { MarketplaceAccessRequestRead } from "../lib/types";
import {
  ApiError,
  fetchMarketplaceAccessRequests,
  fetchMarketplaceProducts,
  requestMarketplaceAccess,
  revokeMarketplaceAccess,
} from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { CrossLinks } from "../components/CrossLinks";
import {
  Button,
  ConfirmDialog,
  CopyLinkButton,
  Empty,
  ErrorState,
  Field,
  Pill,
} from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./MarketplaceScreen.css";

/* ---------------------------------------------------------------------------
   Marketplace — UX-15, the Catalog pattern applied to CX-9's real
   `GET /v1/marketplace/products` (`product_marketplace_api.py::search_marketplace`).

   Same four pieces as `CatalogScreen`:
     1. URL state       q / domain / classification / sort / product
     2. abortable fetch  one in-flight request, aborted on the next filter
     3. virtualization   `VirtualList` (UX-15's generalized `CatalogTable`)
     4. evidence pane    a product's ports + access status, permalinkable by
                         `?product=<version_id>`, requesting access through
                         the real governed `POST .../access-requests` route

   `sort=personalized` (CX-9's own default) ranks by the requester's own
   domain ownership and role -- nothing is ever hidden, only reordered, so
   this screen surfaces `domain_affinity`/`role_affinity` as an honest "why
   this order" rather than leaving the ranking unexplained.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";

const classTone = (c: MarketplaceProductRead["classification"]): Tone =>
  c === "PUBLIC" ? "ok" : c === "INTERNAL" ? "info" : c === "CONFIDENTIAL" ? "warn" : "bad";

const accessTone = (s: MarketplaceProductRead["access_status"]): Tone =>
  s === "ROLE_GRANTED" ? "ok" : s === "REQUEST_APPROVED" ? "ok" : s === "REQUEST_PENDING" ? "warn" : "mute";

const accessLabel = (s: MarketplaceProductRead["access_status"]): string =>
  s === "ROLE_GRANTED"
    ? "granted by role"
    : s === "REQUEST_APPROVED"
      ? "access approved"
      : s === "REQUEST_PENDING"
        ? "request pending"
        : "not requested";


function ProductCard({
  product,
  selected,
  onSelect,
}: {
  product: MarketplaceProductRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`mkt${selected ? " mkt--sel" : ""}`} aria-label={product.name}>
      <button className="mkt__click" onClick={onSelect}>
        <header className="mkt__head">
          <div className="mkt__badges">
            <Pill tone={classTone(product.classification)}>{product.classification.toLowerCase()}</Pill>
            {product.certification_status === "CERTIFIED" ? <Pill tone="ok">certified</Pill> : null}
            <Pill tone={accessTone(product.access_status)}>{accessLabel(product.access_status)}</Pill>
          </div>
          <h3 className="mkt__title">{product.name}</h3>
          <p className="mkt__desc">{product.description}</p>
        </header>
        <div className="mkt__meta">
          <span>{product.domain_name}</span>
          <span>·</span>
          <span>{product.ports.length} port{product.ports.length === 1 ? "" : "s"}</span>
          <span>·</span>
          <span>v{product.version}</span>
          {product.domain_affinity || product.role_affinity ? (
            <span className="mkt__why">
              {product.domain_affinity ? "your domain" : ""}
              {product.domain_affinity && product.role_affinity ? " · " : ""}
              {product.role_affinity ? "matches your role" : ""}
            </span>
          ) : null}
        </div>
      </button>
    </article>
  );
}

function ProductDetail({
  product,
  onClose,
  onRequested,
}: {
  product: MarketplaceProductRead;
  onClose: () => void;
  onRequested: () => void;
}) {
  const [purpose, setPurpose] = useState("");
  const [requesting, setRequesting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  /* R11-B4 — revoking a granted entitlement.

     A marketplace row says *that* access is granted but not which request
     granted it, and revocation addresses the request. So when this product
     shows an approved grant the pane resolves it through the server-scoped
     access-request list (a consumer is narrowed to their own rows there; an
     owner sees the organization's) and offers Revoke against that id.

     Eligibility is deliberately not decided here. `POST .../revoke` requires
     a product-author role and refuses anything that is not currently
     APPROVED, so the button is offered whenever a grant is visible and the
     server's own refusal text is what the user is shown -- rather than this
     screen guessing at a rule it would then have to keep in step. */
  const [grant, setGrant] = useState<MarketplaceAccessRequestRead | null>(null);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeErr, setRevokeErr] = useState<string | null>(null);

  const canRequest = product.access_status === "NOT_REQUESTED";
  const isGranted = product.access_status === "REQUEST_APPROVED";

  useEffect(() => {
    if (!isGranted) {
      setGrant(null);
      return;
    }
    const ac = new AbortController();
    let cancelled = false;
    void (async () => {
      try {
        const page = await fetchMarketplaceAccessRequests({ limit: 100 }, ac.signal);
        if (cancelled) return;
        setGrant(
          page.items.find(
            (r) => r.data_product_version_id === product.id && r.status === "APPROVED",
          ) ?? null,
        );
      } catch {
        /* Without the grant the action is simply not offered. The server is
           the authority on revocation either way, so failing closed here
           costs nothing and never shows an action that cannot work. */
      }
    })();
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [isGranted, product.id]);

  const revoke = useCallback(async () => {
    if (!grant) return;
    setRevoking(true);
    setRevokeErr(null);
    try {
      await revokeMarketplaceAccess(grant.id);
      setConfirmingRevoke(false);
      onRequested();
    } catch (e) {
      setRevokeErr(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setRevoking(false);
    }
  }, [grant, onRequested]);

  const submit = useCallback(async () => {
    if (!purpose.trim()) {
      setErr("A purpose is required to request access.");
      return;
    }
    setRequesting(true);
    setErr(null);
    try {
      await requestMarketplaceAccess(product.id, { purpose, duration_days: 90 });
      onRequested();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setRequesting(false);
    }
  }, [product.id, purpose, onRequested]);



  return (
    <aside className="evp" aria-label={`Detail for ${product.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={product.name}>{product.name}</div>
          <div className="evp__path">{product.product_key} · v{product.version}</div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close detail">×</button>
      </header>
      <div className="evp__body">
        <p className="mkt__d_desc">{product.description}</p>
        <div className="mkt__d_row"><b>Owner</b><span>{product.owner_principal}</span></div>
        <div className="mkt__d_row"><b>Usage terms</b><span>{product.usage_terms}</span></div>
        <div className="mkt__d_row"><b>Quality score</b><span>{product.quality_score != null ? `${Math.round(product.quality_score * 100)}%` : "—"}</span></div>
        <div className="mkt__d_row"><b>Lineage coverage</b><span>{product.lineage_coverage != null ? `${Math.round(product.lineage_coverage * 100)}%` : "—"}</span></div>

        <div className="evp__sub" style={{ marginTop: 14 }}>Ports</div>
        <ol className="evl">
          {product.ports.map((p) => (
            <li key={p.port_key} className="evi evi--info">
              <div className="evi__label">{p.direction} · {p.asset_type.replace(/_/g, " ")}</div>
              <div className="evi__value">{p.name}</div>
              <div className="evi__source">{p.description}</div>
            </li>
          ))}
        </ol>

        <div className="evp__sub" style={{ marginTop: 14 }}>Access</div>
        <p className="mkt__d_desc">
          <Pill tone={accessTone(product.access_status)}>{accessLabel(product.access_status)}</Pill>
        </p>
        {canRequest ? (
          <div className="mkt__req">
            <Field label="Purpose (required)">
              <input
                type="text"
                value={purpose}
                placeholder="why you need access…"
                onChange={(e) => setPurpose(e.target.value)}
              />
            </Field>
            {err ? <p className="mkt__err" role="alert">{err}</p> : null}
            <Button variant="primary" disabled={requesting} onClick={() => void submit()}>
              {requesting ? "Requesting…" : "Request access"}
            </Button>
          </div>
        ) : null}
        {isGranted && grant ? (
          <div className="mkt__revoke">
            <p className="mkt__revoke_hint">
              Revoking denies this principal&rsquo;s next query against the product and records
              who revoked it.
            </p>
            <Button onClick={() => setConfirmingRevoke(true)}>Revoke access</Button>
          </div>
        ) : null}
        {confirmingRevoke && grant ? (
          <ConfirmDialog
            title="Revoke this access grant?"
            description={`${grant.requested_by} will no longer be able to query ${product.name}. Re-granting means a new request and a new approval.`}
            confirmLabel="Revoke access"
            destructive
            busy={revoking}
            error={revokeErr}
            onConfirm={() => void revoke()}
            onCancel={() => {
              setConfirmingRevoke(false);
              setRevokeErr(null);
            }}
          />
        ) : null}
      </div>
      <div className="evp__links">
        {/* The marketplace answers "what may I use". Context products answer
            "what may my agent use" -- the same governance, a different
            consumer, and previously no path between them. */}
        <CrossLinks
          label="Related"
          links={[
            { screen: "context", label: "Context products", title: "Package approved assets for agent consumption" },
            { screen: "developer", label: "Agent gateway", title: "How an external agent reaches approved context" },
          ]}
        />
      </div>
      <footer className="evp__foot">
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/marketplace`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
        <CopyLinkButton target={{ screen: "marketplace", params: { product: product.id } }} />
        <span className="evp__hint">Governed · CX-9</span>
      </footer>
    </aside>
  );
}

export function MarketplaceScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const q = params.get("q") ?? "";
  const domain = params.get("domain") ?? "";
  const classification = params.get("class") ?? "ALL";
  const sort = (params.get("sort") as "personalized" | "catalog") ?? "personalized";
  const selectedId = params.get("product");

  const [items, setItems] = useState<MarketplaceProductRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draftQ, setDraftQ] = useState(q);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchMarketplaceProducts(
        {
          organizationId: ORG,
          q,
          domain: domain || undefined,
          classification: classification !== "ALL" ? classification : undefined,
          sort,
          limit: 50,
          offset: 0,
        },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setItems(page.items);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [q, domain, classification, sort]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const loadMore = useCallback(async () => {
    if (loadingMore || loading || items.length >= (total ?? 0)) return;
    setLoadingMore(true);
    try {
      const page = await fetchMarketplaceProducts({
        organizationId: ORG,
        q,
        domain: domain || undefined,
        classification: classification !== "ALL" ? classification : undefined,
        sort,
        limit: 50,
        offset: items.length,
      });
      setItems((prev) => [...prev, ...page.items]);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, loading, items.length, total, q, domain, classification, sort]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (draftQ !== q) setParams({ q: draftQ || null, product: null });
    }, 250);
    return () => clearTimeout(t);
  }, [draftQ, q, setParams]);

  const selected = useMemo(() => items.find((p) => p.id === selectedId) ?? null, [items, selectedId]);

  return (
    <div className="mktscreen">
      <header className="mktscreen__head">
        <div>
          <h1 className="mktscreen__h1">Marketplace</h1>
          <p className="mktscreen__lede">
            Published, governed data products — ranked by what you own and your role
            when sorted &ldquo;personalized&rdquo; (CX-9), never hidden by it.
          </p>
        </div>
        <div className="mktscreen__stats">
          <span><b className="tnum">{total !== null ? total : "—"}</b> products</span>
        </div>
      </header>

      <div className="mktscreen__filters">
        <Field label="Search">
          <input
            type="search"
            value={draftQ}
            placeholder="name or description…"
            onChange={(e) => setDraftQ(e.target.value)}
          />
        </Field>
        <Field label="Domain">
          <input
            type="text"
            value={domain}
            placeholder="e.g. fin, risk…"
            onChange={(e) => setParams({ domain: e.target.value || null, product: null })}
          />
        </Field>
        <Field label="Classification">
          <select
            value={classification}
            onChange={(e) => setParams({ class: e.target.value === "ALL" ? null : e.target.value, product: null })}
          >
            <option value="ALL">All</option>
            <option value="PUBLIC">Public</option>
            <option value="INTERNAL">Internal</option>
            <option value="CONFIDENTIAL">Confidential</option>
            <option value="RESTRICTED">Restricted</option>
          </select>
        </Field>
        <Field label="Sort">
          <select value={sort} onChange={(e) => setParams({ sort: e.target.value })}>
            <option value="personalized">Personalized</option>
            <option value="catalog">Catalog (alphabetical)</option>
          </select>
        </Field>
      </div>

      <div className="mktscreen__main">
        {error ? (
          <ErrorState title="The marketplace could not be loaded" detail={error} onRetry={() => void load()} />
        ) : loading ? (
          <div className="mktscreen__skeleton" role="status" aria-live="polite">
            Loading marketplace…
          </div>
        ) : (
          <VirtualList
            items={items}
            getKey={(p) => p.id}
            ariaLabel="Marketplace products"
            estimateSize={128}
            totalCount={total}
            onReachEnd={() => void loadMore()}
            loadingMore={loadingMore}
            emptyState={
              <Empty title="No products match these filters" hint="Try clearing the domain or classification filter." />
            }
            renderItem={(p) => (
              <ProductCard product={p} selected={p.id === selectedId} onSelect={() => setParams({ product: p.id })} />
            )}
          />
        )}
        {selected ? (
          <ProductDetail
            product={selected}
            onClose={() => setParams({ product: null })}
            onRequested={() => void load()}
          />
        ) : null}
      </div>
    </div>
  );
}
