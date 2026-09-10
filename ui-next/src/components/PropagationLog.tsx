import { Pill } from "./primitives";
import "./PropagationLog.css";

/* ---------------------------------------------------------------------------
   When a quality incident or a classification spreads across lineage, the
   platform makes a decision the user did not make — an asset they never
   touched becomes blocked or becomes PII. ADR-0016 has quality fail closed,
   which means a tool call can be refused because of a check on a table three
   hops upstream.

   A user cannot accept that unless they can see the chain, so every hop states
   the MECHANISM that carried it ("via column lineage", "reads from"), not just
   that it happened. "Affected" is a claim; "affected via column lineage from
   raw_sales" is an argument.

   PROVENANCE IS PART OF THE ARGUMENT (review 2026-09-05, D02). The only
   caller today passes a hand-written four-step story about `orders_raw`; no
   endpoint, fixture generator or lineage walk produces it, and none exists
   (see `ReviewQueueScreen`'s AT-D4 note). It is gated off by default, but a
   build that turns the gate on must not be able to render it as though the
   platform had traversed anything. `illustrative` is therefore not
   decoration: it labels the steps on screen and in the accessible name, so a
   steward reading it knows it is an example of the mechanism rather than
   evidence about their estate. A real, evidence-backed propagation read model
   renders the same component with the flag off.
--------------------------------------------------------------------------- */

export interface PropagationStep {
  kind: "origin" | "hop" | "blocked";
  /** What happened. */
  text: string;
  /** How it travelled — the edge kind or rule that carried it. Origin steps
   *  have no mechanism because nothing carried them; they are where it started. */
  mechanism?: string;
}

export function PropagationLog({
  title,
  steps,
  illustrative = false,
  illustrativeNote,
}: {
  title: string;
  steps: PropagationStep[];
  /** True when these steps are a worked example, not traversed evidence. */
  illustrative?: boolean;
  /** What is missing, so the reader knows what would replace it. */
  illustrativeNote?: string;
}) {
  return (
    <section
      className={`plog${illustrative ? " plog--demo" : ""}`}
      aria-label={illustrative ? `${title} (worked example, not live evidence)` : title}
    >
      <h4 className="plog__h">
        {title}
        {illustrative ? <Pill tone="warn">worked example · not your data</Pill> : null}
      </h4>
      {illustrative ? (
        <p className="plog__demo">
          {illustrativeNote ??
            "These steps are hard-coded to show how propagation is reported. Nothing here was traversed against your estate."}
        </p>
      ) : null}
      <ol className="plog__l">
        {steps.map((s, i) => (
          <li key={i} className={`ps ps--${s.kind}`}>
            <span className="ps__g" aria-hidden="true">
              {s.kind === "origin" ? "●" : s.kind === "blocked" ? "✕" : "→"}
            </span>
            <span className="ps__b">
              <span className="ps__t">{s.text}</span>
              {s.mechanism ? <span className="ps__m">{s.mechanism}</span> : null}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
