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
}: {
  title: string;
  steps: PropagationStep[];
}) {
  return (
    <section className="plog" aria-label={title}>
      <h4 className="plog__h">{title}</h4>
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
