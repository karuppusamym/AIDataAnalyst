import { useCallback, useEffect, useMemo, useState } from "react";
import {
  bulkDecideParsedLineageEdges,
  decideParsedLineageEdge,
  listParsedLineageReviewQueue,
} from "../lib/api";
import type { ParsedLineageEdgeReviewQueueItemRead } from "../lib/types";
import type {
  ParsedLineageEdgeDecision,
  ParsedLineageEdgeType,
} from "../lib/ui-types";
import {
  Button,
  Empty,
  ErrorState,
  Field,
  Pill,
} from "../components/primitives";

/* ---------------------------------------------------------------------------
   P1-05 / ADR-0026 — parsed-lineage-edge review queue.

   First-cut, functional table view of PROPOSED lineage edges across the
   five non-governed parser-produced edge tables (view / procedure /
   dbt-column / OpenLineage-table / OpenLineage-column). Approve / Reject
   post to the same maker-checker endpoint the RelationshipCandidate
   review flow uses; bulk-decide is wired but the toolbar exposes only the
   single-item path in this first cut. Filter by edge_type + confidence.
--------------------------------------------------------------------------- */

const EDGE_TYPES: ParsedLineageEdgeType[] = [
  "VIEW",
  "PROCEDURE",
  "DBT",
  "OPENLINEAGE_TABLE",
  "OPENLINEAGE_COLUMN",
];

const CONFIDENCE_STRING_TO_FLOAT: Record<string, number> = {
  FULL: 1.0,
  PARTIAL: 0.6,
  LOW: 0.3,
};

function confidenceDisplay(raw: string | number | null): string {
  if (raw == null) return "—";
  if (typeof raw === "number") return raw.toFixed(2);
  return raw;
}

function confidenceFloat(raw: string | number | null): number | null {
  if (raw == null) return null;
  if (typeof raw === "number") return raw;
  const key = String(raw).toUpperCase();
  return CONFIDENCE_STRING_TO_FLOAT[key] ?? null;
}

export function ParsedLineageReviewScreen() {
  const [items, setItems] = useState<ParsedLineageEdgeReviewQueueItemRead[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [edgeType, setEdgeType] = useState<ParsedLineageEdgeType | "">("");
  const [minConfidence, setMinConfidence] = useState<string>("");
  const [inflight, setInflight] = useState<string | null>(null);
  const [ackMessage, setAckMessage] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const parsed = minConfidence ? Number(minConfidence) : null;
        const result = await listParsedLineageReviewQueue(
          {
            edgeType: edgeType || null,
            minConfidence: parsed != null && !Number.isNaN(parsed) ? parsed : null,
            limit: 100,
            offset: 0,
          },
          signal,
        );
        setItems(result.items);
        setTotal(result.total);
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load review queue");
      } finally {
        setLoading(false);
      }
    },
    [edgeType, minConfidence],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const decide = useCallback(
    async (
      item: ParsedLineageEdgeReviewQueueItemRead,
      decision: ParsedLineageEdgeDecision,
    ) => {
      const reason = window.prompt(
        `Reason for ${decision.toLowerCase()} on ${item.edge_type} edge?`,
      );
      if (!reason) return;
      setInflight(item.edge_id);
      try {
        await decideParsedLineageEdge(item.edge_id, {
          edge_type: item.edge_type,
          decision,
          reason,
        });
        setAckMessage(
          `${item.edge_type} edge ${decision === "APPROVED" ? "approved" : "rejected"}.`,
        );
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Decision failed");
      } finally {
        setInflight(null);
      }
    },
    [load],
  );

  const summary = useMemo(
    () => `${items.length} shown of ${total} total PROPOSED edges`,
    [items.length, total],
  );

  return (
    <section aria-labelledby="parsed-lineage-review-title" style={{ padding: "1rem" }}>
      <header style={{ marginBottom: "1rem" }}>
        <h1 id="parsed-lineage-review-title">Parsed lineage review</h1>
        <p style={{ maxWidth: "60ch" }}>
          PROPOSED lineage edges from the five non-governed parsers — view,
          procedure, dbt, OpenLineage table, OpenLineage column. Approve to
          fold into the shared graph; reject to keep out and record why.
          Maker-checker enforced: you cannot decide an edge you created.
        </p>
      </header>

      <div
        style={{ display: "flex", gap: "0.5rem", alignItems: "end", marginBottom: "0.75rem" }}
      >
        <Field label="Edge type">
          <select
            value={edgeType}
            onChange={(event) => setEdgeType(event.target.value as ParsedLineageEdgeType | "")}
          >
            <option value="">All</option>
            {EDGE_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Min confidence">
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={minConfidence}
            onChange={(event) => setMinConfidence(event.target.value)}
            placeholder="0.0 - 1.0"
          />
        </Field>
        <Button onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {ackMessage ? (
        <div role="status" style={{ marginBottom: "0.75rem" }}>
          {ackMessage}
        </div>
      ) : null}

      {error ? <ErrorState title="Could not load queue" detail={error} onRetry={() => void load()} /> : null}

      {!loading && items.length === 0 && !error ? (
        <Empty
          title="No proposed lineage edges"
          hint="Nothing waiting for review right now."
        />
      ) : null}

      {items.length > 0 ? (
        <>
          <div style={{ margin: "0.5rem 0" }}>{summary}</div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left" }}>
                <th>Type</th>
                <th>Source</th>
                <th>Target</th>
                <th>Transformation</th>
                <th>Confidence</th>
                <th>Author</th>
                <th>Source SQL</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const confidenceHint = confidenceFloat(item.confidence);
                return (
                  <tr key={item.edge_id} style={{ borderTop: "1px solid #eee" }}>
                    <td>
                      <Pill>{item.edge_type}</Pill>
                    </td>
                    <td>
                      <code>{item.source_label}</code>
                    </td>
                    <td>
                      <code>{item.target_label}</code>
                    </td>
                    <td>{item.transformation_type ?? "—"}</td>
                    <td
                      title={
                        confidenceHint != null
                          ? `Coerced to ${confidenceHint.toFixed(2)}`
                          : undefined
                      }
                    >
                      {confidenceDisplay(item.confidence)}
                    </td>
                    <td>{item.created_by ?? "—"}</td>
                    <td>
                      <small>
                        {item.source_sql_reference.kind}
                        {Object.entries(item.source_sql_reference)
                          .filter(([k]) => k !== "kind")
                          .map(([k, v]) => (
                            <div key={k}>
                              {k}: <code>{v}</code>
                            </div>
                          ))}
                      </small>
                    </td>
                    <td>
                      <Button
                        onClick={() => void decide(item, "APPROVED")}
                        disabled={inflight === item.edge_id}
                      >
                        Approve
                      </Button>{" "}
                      <Button
                        onClick={() => void decide(item, "REJECTED")}
                        disabled={inflight === item.edge_id}
                      >
                        Reject
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      ) : null}

      {/* Bulk-decide is intentionally not wired to a control here yet --
          the helper is exported so a follow-up can add multi-select. */}
      <div style={{ display: "none" }}>
        <button
          type="button"
          onClick={() =>
            void bulkDecideParsedLineageEdges({
              items: [],
              decision: "APPROVED",
              reason: "n/a",
            })
          }
        >
          bulk placeholder
        </button>
      </div>
    </section>
  );
}
