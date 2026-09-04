import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AssetDescriptionDraftRead, AssetDescriptionDraftStatus } from "../lib/types";
import {
  ApiError,
  classifyDescriptionDraftError,
  listAssetDescriptionDrafts,
  submitAssetDescriptionDraft,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./DescriptionDraftsScreen.css";

/* ---------------------------------------------------------------------------
   P1-04: Asset description drafts.

   Backend has had `POST .../asset-description-drafts/generate` (batch) and
   `POST /asset-description-drafts/{draft_id}/submit` (moves DRAFT ->
   PENDING_APPROVAL) since AT-13, but ui-next had zero references to either
   -- drafts could only be created and moved to PENDING_APPROVAL via `curl`.
   This screen is the smallest thing that closes that loop: a steward can see
   the drafts the batch generator produced, submit ones that pass the
   deterministic 0.4 evidence bar, and hand off to the ReviewQueueScreen the
   moment they flip to PENDING_APPROVAL.

   Ordering: the server orders by `overall_score DESC, created_at DESC` --
   the reviewer-priority order the API's own module comment calls out ("the
   *only* effect of the score on review order"). We default to that same
   order to stay honest to the API's intent.

   The 0.4 gate (`asset_description_service.ensure_reviewable`) is enforced
   server-side; the Submit button here disables below the threshold as a
   visual affordance only, and the classified error is surfaced verbatim
   when it fires anyway (e.g. after a re-score narrows the gap).
--------------------------------------------------------------------------- */

const STATUS_FILTERS: Array<{ value: "ALL" | AssetDescriptionDraftStatus; label: string }> = [
  { value: "ALL", label: "All" },
  { value: "DRAFT", label: "Draft" },
  { value: "PENDING_APPROVAL", label: "Pending approval" },
  { value: "APPROVED", label: "Approved" },
  { value: "REJECTED", label: "Rejected" },
];

const SORT_OPTIONS: Array<{ value: "score_desc" | "created_desc"; label: string }> = [
  { value: "score_desc", label: "Best evidence first" },
  { value: "created_desc", label: "Most recent first" },
];

/** Minimum overall_score the server accepts for submit (mirrors
 *  `MINIMUM_EVIDENCE_FOR_REVIEW = 0.4` in asset_description_service.py). */
export const MINIMUM_EVIDENCE_FOR_REVIEW = 0.4;

const DESCRIPTION_SNIPPET_MAX = 120;

const statusTone = (status: string): Tone => {
  switch (status) {
    case "APPROVED":
      return "ok";
    case "PENDING_APPROVAL":
      return "info";
    case "REJECTED":
      return "bad";
    case "DRAFT":
    default:
      return "mute";
  }
};

const scoreTone = (score: number): Tone => {
  if (score >= 0.7) return "ok";
  if (score >= MINIMUM_EVIDENCE_FOR_REVIEW) return "warn";
  return "bad";
};

const pct = (score: number) => `${Math.round(score * 100)}%`;

const relTime = (iso: string): string => {
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

const snippet = (text: string, max = DESCRIPTION_SNIPPET_MAX): string => {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max - 1)}…`;
};

interface DraftRowProps {
  draft: AssetDescriptionDraftRead;
  expanded: boolean;
  submitting: boolean;
  submitError: string | null;
  onToggleExpand: () => void;
  onSubmit: () => void;
  onOpenReview: () => void;
}

function DraftRow({
  draft,
  expanded,
  submitting,
  submitError,
  onToggleExpand,
  onSubmit,
  onOpenReview,
}: DraftRowProps) {
  const belowGate = draft.overall_score < MINIMUM_EVIDENCE_FOR_REVIEW;
  const scoreTitle =
    `accuracy ${pct(draft.accuracy_score)} · clarity ${pct(draft.clarity_score)} · ` +
    `style ${pct(draft.style_score)} · completeness ${pct(draft.completeness_score)}`;

  return (
    <div className={`draftrow${expanded ? " draftrow--exp" : ""}`}>
      <div className="draftrow__head">
        <div className="draftrow__name">
          <button
            type="button"
            className="draftrow__toggle"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            aria-controls={`draftrow-body-${draft.id}`}
          >
            <span aria-hidden="true" className="draftrow__caret">
              {expanded ? "▾" : "▸"}
            </span>
            <span className="draftrow__tablename">{draft.table_name}</span>
          </button>
          <div className="draftrow__snippet" title={draft.drafted_text}>
            {snippet(draft.drafted_text)}
          </div>
        </div>
        <div className="draftrow__score" title={scoreTitle}>
          <Pill tone={scoreTone(draft.overall_score)}>{pct(draft.overall_score)}</Pill>
        </div>
        <div className="draftrow__status">
          <Pill tone={statusTone(draft.status)}>
            {draft.status.toLowerCase().replace(/_/g, " ")}
          </Pill>
        </div>
        <div className="draftrow__when tnum" title={new Date(draft.created_at).toLocaleString()}>
          {relTime(draft.created_at)}
        </div>
        <div className="draftrow__actions">
          {draft.status === "DRAFT" ? (
            <Button
              variant="primary"
              onClick={onSubmit}
              disabled={belowGate || submitting}
              title={
                belowGate
                  ? "Not enough evidence to review (needs overall score ≥ 40%)"
                  : "Send this draft to the governance review queue"
              }
            >
              {submitting ? "Submitting…" : "Submit for review"}
            </Button>
          ) : draft.status === "PENDING_APPROVAL" ? (
            <Button onClick={onOpenReview}>Open in review queue</Button>
          ) : null}
        </div>
      </div>
      {expanded ? (
        <div id={`draftrow-body-${draft.id}`} className="draftrow__body">
          <div className="draftrow__scores" aria-label="Score breakdown">
            <span><b>{pct(draft.accuracy_score)}</b> accuracy</span>
            <span><b>{pct(draft.clarity_score)}</b> clarity</span>
            <span><b>{pct(draft.style_score)}</b> style</span>
            <span><b>{pct(draft.completeness_score)}</b> completeness</span>
          </div>
          <div className="draftrow__full" aria-label="Full draft text">
            {draft.drafted_text}
          </div>
          {submitError ? (
            <div className="draftrow__err" role="alert">
              {submitError}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function DescriptionDraftsScreen() {
  const ORG = useOrgId();

  const [drafts, setDrafts] = useState<AssetDescriptionDraftRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tableQuery, setTableQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"ALL" | AssetDescriptionDraftStatus>("ALL");
  const [sort, setSort] = useState<"score_desc" | "created_desc">("score_desc");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [submitting, setSubmitting] = useState<ReadonlySet<string>>(new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  const inflight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    setLoading(true);
    setError(null);
    try {
      const page = await listAssetDescriptionDrafts(
        ORG,
        statusFilter === "ALL" ? { limit: 200 } : { status: statusFilter, limit: 200 },
        ac.signal,
      );
      setDrafts(page.drafts);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      const detail =
        e instanceof ApiError
          ? classifyDescriptionDraftError(e).detail
          : (e as Error).message;
      setError(detail);
    } finally {
      setLoading(false);
    }
  }, [ORG, statusFilter]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const visibleDrafts = useMemo(() => {
    const needle = tableQuery.trim().toLowerCase();
    let out = drafts;
    if (needle) {
      out = out.filter((d) => d.table_name.toLowerCase().includes(needle));
    }
    const sorted = [...out];
    if (sort === "score_desc") {
      sorted.sort(
        (a, b) =>
          b.overall_score - a.overall_score ||
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      );
    } else {
      sorted.sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      );
    }
    return sorted;
  }, [drafts, tableQuery, sort]);

  const toggleExpand = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const openReview = useCallback((draft: AssetDescriptionDraftRead) => {
    const params = new URLSearchParams();
    params.set("type", "ASSET_DESCRIPTION_DRAFT");
    if (draft.governance_review_id) params.set("focus", draft.governance_review_id);
    history.pushState(null, "", `?${params.toString()}#/governance`);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }, []);

  const submit = useCallback(async (draft: AssetDescriptionDraftRead) => {
    setSubmitting((prev) => {
      const next = new Set(prev);
      next.add(draft.id);
      return next;
    });
    setRowErrors((prev) => {
      if (!(draft.id in prev)) return prev;
      const next = { ...prev };
      delete next[draft.id];
      return next;
    });
    // Optimistically flip status so the row moves out of DRAFT immediately;
    // rolled back below if the server refuses.
    setDrafts((prev) =>
      prev.map((d) =>
        d.id === draft.id ? { ...d, status: "PENDING_APPROVAL" } : d,
      ),
    );
    try {
      const review = await submitAssetDescriptionDraft(draft.id);
      setDrafts((prev) =>
        prev.map((d) =>
          d.id === draft.id
            ? { ...d, status: "PENDING_APPROVAL", governance_review_id: review.id }
            : d,
        ),
      );
    } catch (e) {
      // Roll back the optimistic flip on any failure.
      setDrafts((prev) =>
        prev.map((d) => (d.id === draft.id ? { ...d, status: draft.status } : d)),
      );
      const detail =
        e instanceof ApiError
          ? classifyDescriptionDraftError(e).detail
          : (e as Error).message;
      setRowErrors((prev) => ({ ...prev, [draft.id]: detail }));
      // Auto-expand the row so the error is visible without a click.
      setExpanded((prev) => {
        if (prev.has(draft.id)) return prev;
        const next = new Set(prev);
        next.add(draft.id);
        return next;
      });
    } finally {
      setSubmitting((prev) => {
        const next = new Set(prev);
        next.delete(draft.id);
        return next;
      });
    }
  }, []);

  const goToCatalog = useCallback(() => {
    if (location.hash !== "#/catalog") history.pushState(null, "", "#/catalog");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }, []);

  return (
    <div className="drafts">
      <header className="drafts__head">
        <div>
          <h1 className="drafts__h1">Description drafts</h1>
          <p className="drafts__lede">
            Model-drafted asset descriptions. Submit a draft for governance review
            once it clears the evidence bar; drafts below the bar stay parked
            here until more evidence lands.
          </p>
        </div>
      </header>

      <div className="drafts__filters">
        <Field label="Table name">
          <input
            type="search"
            placeholder="filter by table…"
            value={tableQuery}
            onChange={(e) => setTableQuery(e.target.value)}
          />
        </Field>
        <Field label="Status">
          <select
            value={statusFilter}
            onChange={(e) =>
              setStatusFilter(e.target.value as "ALL" | AssetDescriptionDraftStatus)
            }
          >
            {STATUS_FILTERS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </Field>
        <Field label="Sort">
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as "score_desc" | "created_desc")}
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </Field>
      </div>

      <div className="drafts__main">
        {error ? (
          <ErrorState
            title="Description drafts could not be loaded"
            detail={error}
            onRetry={() => void load()}
          />
        ) : loading ? (
          <div className="drafts__skeleton" role="status" aria-live="polite">
            Loading description drafts…
          </div>
        ) : visibleDrafts.length === 0 ? (
          drafts.length === 0 ? (
            <div className="drafts__empty">
              <Empty
                title="No drafts yet"
                hint="Generate drafts from the Catalog screen and they will appear here."
              />
              <Button onClick={goToCatalog}>Go to Catalog</Button>
            </div>
          ) : (
            <Empty
              title="No drafts match these filters"
              hint="Clear the table filter or switch the status filter to see more."
            />
          )
        ) : (
          <div className="drafts__list" role="list" aria-label="Description drafts">
            <div className="drafts__legend" aria-hidden="true">
              <span>Asset</span>
              <span>Score</span>
              <span>Status</span>
              <span>Created</span>
              <span>Action</span>
            </div>
            {visibleDrafts.map((d) => (
              <DraftRow
                key={d.id}
                draft={d}
                expanded={expanded.has(d.id)}
                submitting={submitting.has(d.id)}
                submitError={rowErrors[d.id] ?? null}
                onToggleExpand={() => toggleExpand(d.id)}
                onSubmit={() => void submit(d)}
                onOpenReview={() => openReview(d)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
