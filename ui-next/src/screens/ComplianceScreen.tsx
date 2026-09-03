import { useCallback, useEffect, useRef, useState } from "react";
import type { CompliancePackRead, GeneratePackRequest } from "../lib/types";
import { ApiError, downloadCompliancePack, fetchCompliancePacks, generateCompliancePack } from "../lib/api";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ComplianceScreen.css";

/* ---------------------------------------------------------------------------
   Compliance packs — the pack-list half of the legacy Enterprise Control
   Center's `renderCompliance` (`ui/scripts/features/control-center.js`),
   rebuilt against the real, already-merged `compliance_api.py`
   (Phase E, EE.4/OB-5). The other half of that legacy function — AI
   refusals — is already fully covered by `LineageRefusalScreen` and is
   deliberately out of scope here.

   Org-wide, like `AuditLedgerScreen`: all three routes below derive the
   acting organization from the auth context server-side
   (`context.require_organization()`, `compliance_api.py:74/128/152/181`)
   rather than an `organization_id` path segment, so there is no
   datasource/org picker on this screen.

   Two pieces:
     1. Generate pack form  framework / period start & end (`datetime-local`,
                             round-tripped through `Date` exactly like
                             `AuditLedgerScreen`'s `localInputToIso`, so the
                             naive picker value becomes the timezone-aware
                             ISO string `GeneratePackRequest` requires) /
                             optional name. The endpoint's own 422 (period_end
                             not after period_start) is surfaced verbatim,
                             not re-derived client-side.
     2. Pack list           name, framework, status, generated-at (relative
                             time — `relTime()`, the same hand-rolled-
                             per-screen convention `QualityScreen` uses
                             rather than a shared date-math helper) and a
                             per-row "Download evidence" action that fetches
                             and renders the raw JSON evidence body in a
                             `<pre>` block, collapsed until asked for.

   `download_compliance_pack` is deliberately narrower than
   `list_compliance_packs`/`get_compliance_pack` — it excludes `Viewer`
   (`compliance_api.py:181` vs `:128`/`:152`) — so a Viewer's 403 on
   "Download evidence" is the route working as designed, not a bug: it
   renders as an inline error scoped to that one row, the same detail
   string every other failed call in this app surfaces.
--------------------------------------------------------------------------- */

const FRAMEWORKS: GeneratePackRequest["framework"][] = [
  "MODEL_RISK",
  "BCBS_239",
  "ACCESS_REVIEW",
  "AI_USAGE",
  "CHANGE_CONTROL",
];

function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ");
}

const relTime = (iso: string | null): string => {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

const statusTone = (status: string): Tone =>
  status === "COMPLETE" ? "ok" : status === "PENDING" ? "warn" : status === "FAILED" ? "bad" : "mute";

/** Same round-trip `AuditLedgerScreen.localInputToIso` uses: a bare
 *  `datetime-local` value carries no timezone of its own, so parsing it
 *  through `Date` (browser-local) and re-emitting with `.toISOString()` is
 *  what turns it into the timezone-aware UTC string the backend requires. */
function localInputToIso(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function defaultLocalDateTime(offsetDays: number): string {
  const d = new Date(Date.now() + offsetDays * 86_400_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

interface EvidenceState {
  loading: boolean;
  error: string | null;
  data: Record<string, unknown> | null;
}

function PackRow({
  pack,
  evidence,
  onToggle,
}: {
  pack: CompliancePackRead;
  evidence: EvidenceState | undefined;
  onToggle: () => void;
}) {
  const buttonLabel =
    evidence === undefined ? "Download evidence" : evidence.loading ? "Loading…" : "Hide evidence";
  return (
    <>
      <tr>
        <td>
          <span className="cpk__name">{pack.name}</span>
          <span className="cpk__id">{pack.id}</span>
        </td>
        <td><Pill tone="mute">{humanize(pack.framework)}</Pill></td>
        <td><Pill tone={statusTone(pack.status)}>{humanize(pack.status)}</Pill></td>
        <td>{relTime(pack.generated_at)}</td>
        <td>
          <Button disabled={evidence?.loading} onClick={onToggle}>
            {buttonLabel}
          </Button>
        </td>
      </tr>
      {evidence !== undefined ? (
        <tr>
          <td colSpan={5}>
            {evidence.error ? (
              <div className="cpk__err" role="alert">{evidence.error}</div>
            ) : evidence.data ? (
              <pre className="cpk__pre">{JSON.stringify(evidence.data, null, 2)}</pre>
            ) : null}
          </td>
        </tr>
      ) : null}
    </>
  );
}

const INITIAL_FORM = {
  framework: "MODEL_RISK" as GeneratePackRequest["framework"],
  periodStart: defaultLocalDateTime(-30),
  periodEnd: defaultLocalDateTime(0),
  name: "",
};

export function ComplianceScreen() {
  const [packs, setPacks] = useState<CompliancePackRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evidenceById, setEvidenceById] = useState<Record<string, EvidenceState>>({});

  const [form, setForm] = useState(INITIAL_FORM);
  const [generating, setGenerating] = useState(false);
  const [generateStatus, setGenerateStatus] = useState<{ text: string; kind: "success" | "error" } | null>(null);

  const inflight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    setLoading(true);
    setError(null);
    try {
      const page = await fetchCompliancePacks({ limit: 100, offset: 0 }, ac.signal);
      setPacks(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const setField = useCallback(<K extends keyof typeof INITIAL_FORM>(key: K, value: (typeof INITIAL_FORM)[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  }, []);

  const submitGenerate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const periodStart = localInputToIso(form.periodStart);
      const periodEnd = localInputToIso(form.periodEnd);
      if (!periodStart || !periodEnd) {
        setGenerateStatus({ text: "Period start and end must both be valid dates.", kind: "error" });
        return;
      }
      setGenerating(true);
      setGenerateStatus(null);
      try {
        await generateCompliancePack({
          framework: form.framework,
          period_start: periodStart,
          period_end: periodEnd,
          name: form.name.trim() || null,
        });
        setGenerateStatus({ text: "Compliance pack generated and archived.", kind: "success" });
        setForm((f) => ({ ...INITIAL_FORM, framework: f.framework }));
        await load();
      } catch (err) {
        setGenerateStatus({ text: err instanceof ApiError ? err.detail : (err as Error).message, kind: "error" });
      } finally {
        setGenerating(false);
      }
    },
    [form, load],
  );

  const toggleEvidence = useCallback(async (pack: CompliancePackRead) => {
    if (evidenceById[pack.id] !== undefined) {
      setEvidenceById((prev) => {
        const next = { ...prev };
        delete next[pack.id];
        return next;
      });
      return;
    }
    setEvidenceById((prev) => ({ ...prev, [pack.id]: { loading: true, error: null, data: null } }));
    try {
      const data = await downloadCompliancePack(pack.id);
      setEvidenceById((prev) => ({ ...prev, [pack.id]: { loading: false, error: null, data } }));
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : (err as Error).message;
      setEvidenceById((prev) => ({ ...prev, [pack.id]: { loading: false, error: detail, data: null } }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evidenceById]);

  return (
    <div className="cplx">
      <header className="cplx__head">
        <div>
          <h1 className="cplx__h1">Compliance packs</h1>
          <p className="cplx__lede">
            Generate audit-ready evidence bundles from runtime evidence, then download each
            pack's structured JSON body for the framework's own review process.
          </p>
        </div>
      </header>

      <article className="cplx__panel">
        <h2 className="cplx__h2">Generate pack</h2>
        <form onSubmit={(e) => void submitGenerate(e)}>
          <div className="cplx__grid">
            <Field label="Framework">
              <select
                value={form.framework}
                onChange={(e) => setField("framework", e.target.value as GeneratePackRequest["framework"])}
              >
                {FRAMEWORKS.map((f) => (
                  <option key={f} value={f}>{humanize(f)}</option>
                ))}
              </select>
            </Field>
            <Field label="Period start">
              <input
                type="datetime-local"
                required
                value={form.periodStart}
                onChange={(e) => setField("periodStart", e.target.value)}
              />
            </Field>
            <Field label="Period end">
              <input
                type="datetime-local"
                required
                value={form.periodEnd}
                onChange={(e) => setField("periodEnd", e.target.value)}
              />
            </Field>
            <Field label="Name (optional)">
              <input
                placeholder="Auto-named from framework and period"
                value={form.name}
                onChange={(e) => setField("name", e.target.value)}
              />
            </Field>
          </div>
          <Button type="submit" variant="primary" disabled={generating}>
            {generating ? "Generating…" : "Generate pack"}
          </Button>
        </form>
        {generateStatus ? (
          <div className={`cplx__status cplx__status--${generateStatus.kind}`} role="status">
            {generateStatus.text}
          </div>
        ) : null}
      </article>

      <article className="cplx__panel cplx__panel--grow">
        <h2 className="cplx__h2">Generated packs</h2>
        {loading && packs.length === 0 ? (
          <div className="cplx__loading" role="status">Loading compliance packs…</div>
        ) : error ? (
          <ErrorState detail={error} onRetry={() => void load()} title="Compliance packs could not be loaded" />
        ) : packs.length === 0 ? (
          <Empty title="No compliance packs yet" hint="Generate one above to produce the first audit-ready evidence bundle." />
        ) : (
          <div className="cplx__tablewrap">
            <table className="cplx__table">
              <thead>
                <tr>
                  <th>Pack</th>
                  <th>Framework</th>
                  <th>Status</th>
                  <th>Generated</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {packs.map((pack) => (
                  <PackRow
                    key={pack.id}
                    pack={pack}
                    evidence={evidenceById[pack.id]}
                    onToggle={() => void toggleEvidence(pack)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </article>
    </div>
  );
}
