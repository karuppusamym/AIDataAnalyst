import { useCallback, useEffect, useState } from "react";
import type {
  AiAssessmentTemplateRead,
  AiAssetVersionRead,
  AiRemediationRead,
  AiTrustScoreRead,
} from "../lib/types";
import {
  ApiError,
  fetchAiAssessmentTemplates,
  fetchAiAssets,
  fetchAiAssetTrust,
  fetchAiRemediations,
  updateAiRemediation,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./AiRegistryScreen.css";

/* ---------------------------------------------------------------------------
   AI registry (module 15 / CP-7,CP-8).

   The backend has carried AI assets, deterministic trust scoring and the
   findings-to-remediation loop since the AI-trust slice; the React portal had
   the types but no screen, so none of it was reachable. This screen closes
   that loop: pick an AI asset, see its trust grade and per-factor breakdown
   with blocking findings, and work its remediations (advance status, or accept
   the risk — the latter enforced to an independent role server-side). The
   built-in assessment frameworks an assessment is seeded from are shown for
   reference.
--------------------------------------------------------------------------- */

const pct = (n: number) => `${Math.round(n * 100)}%`;

const REMEDIATION_STATES = ["OPEN", "IN_PROGRESS", "RESOLVED", "ACCEPTED_RISK"] as const;

function gradeTone(grade: AiTrustScoreRead["grade"]): Tone {
  return grade === "TRUSTED" ? "ok" : grade === "CONDITIONAL" ? "warn" : "bad";
}

function riskTone(tier: AiAssetVersionRead["risk_tier"]): Tone {
  return tier === "LOW" ? "ok" : tier === "MEDIUM" ? "warn" : "bad";
}

function statusTone(status: string): Tone {
  return status === "RESOLVED" ? "ok" : status === "ACCEPTED_RISK" ? "accent" : "warn";
}

export function AiRegistryScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const selectedId = params.get("ai");

  const [assets, setAssets] = useState<AiAssetVersionRead[]>([]);
  const [assetsLoading, setAssetsLoading] = useState(true);
  const [assetsError, setAssetsError] = useState<string | null>(null);

  const [trust, setTrust] = useState<AiTrustScoreRead | null>(null);
  const [remediations, setRemediations] = useState<AiRemediationRead[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);

  const [templates, setTemplates] = useState<AiAssessmentTemplateRead[] | null>(null);

  const loadAssets = useCallback(async () => {
    setAssetsLoading(true);
    setAssetsError(null);
    try {
      const page = await fetchAiAssets(ORG);
      setAssets(page.items);
    } catch (e) {
      setAssetsError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setAssetsLoading(false);
    }
  }, [ORG]);

  useEffect(() => {
    void loadAssets();
  }, [loadAssets]);

  const loadDetail = useCallback(async (versionId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    setTrust(null);
    setRemediations([]);
    try {
      const [t, r] = await Promise.all([
        fetchAiAssetTrust(versionId),
        fetchAiRemediations(versionId),
      ]);
      setTrust(t);
      setRemediations(r.items);
    } catch (e) {
      setDetailError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const selected = assets.find((a) => a.id === selectedId) ?? null;

  const changeStatus = useCallback(
    async (remediationId: string, status: (typeof REMEDIATION_STATES)[number]) => {
      setSavingId(remediationId);
      setDetailError(null);
      try {
        const updated = await updateAiRemediation(remediationId, { status });
        setRemediations((prev) => prev.map((r) => (r.id === remediationId ? updated : r)));
        // Trust depends on open findings, so refresh it after a status change.
        if (selectedId) void fetchAiAssetTrust(selectedId).then(setTrust).catch(() => {});
      } catch (e) {
        setDetailError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setSavingId(null);
      }
    },
    [selectedId],
  );

  const loadTemplates = useCallback(async () => {
    if (templates !== null) return;
    try {
      setTemplates(await fetchAiAssessmentTemplates());
    } catch {
      setTemplates([]);
    }
  }, [templates]);

  return (
    <div className="air">
      <header className="air__head">
        <h1>AI registry</h1>
        <p>
          Every governed AI asset, its deterministic trust grade, and the findings-to-remediation
          loop that gates it. Trust is computed from approval, assessment, evaluation evidence and
          open findings — never asserted.
        </p>
      </header>

      <div className="air__body">
        <aside className="air__list" aria-label="AI assets">
          {assetsError ? (
            <ErrorState title="AI assets could not be loaded" detail={assetsError} onRetry={() => void loadAssets()} />
          ) : assetsLoading ? (
            <p className="air__load" role="status">Loading AI assets…</p>
          ) : assets.length === 0 ? (
            <Empty title="No AI assets yet" hint="Registered AI agents and models appear here once created." />
          ) : (
            <ul className="air__assets">
              {assets.map((a) => (
                <li key={a.id}>
                  <button
                    className={`air__asset ${a.id === selectedId ? "is-active" : ""}`}
                    aria-current={a.id === selectedId ? "true" : undefined}
                    onClick={() => setParams({ ai: a.id })}
                  >
                    <span className="air__aname">{a.name}</span>
                    <span className="air__ameta">
                      <Pill tone="mute">{a.asset_kind.toLowerCase()}</Pill>
                      <Pill tone={riskTone(a.risk_tier)}>{a.risk_tier.toLowerCase()} risk</Pill>
                      <span className="air__prov">{a.provider_type}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="air__detail" aria-label="AI asset detail">
          {!selected ? (
            <Empty title="Select an AI asset" hint="Pick an asset to see its trust grade and remediations." />
          ) : detailError ? (
            <ErrorState title="This asset's governance detail could not be loaded" detail={detailError} onRetry={() => void loadDetail(selected.id)} />
          ) : (
            <>
              <div className="air__dhead">
                <div>
                  <h2>{selected.name}</h2>
                  <p className="air__dsub">
                    v{selected.version} · {selected.status.toLowerCase()} · owner {selected.owner_principal}
                  </p>
                </div>
                <Pill tone={riskTone(selected.risk_tier)}>{selected.risk_tier.toLowerCase()} risk</Pill>
              </div>

              <div className="air__card">
                <div className="air__cardhead">
                  <span className="air__csub">Trust</span>
                  {trust ? <Pill tone={gradeTone(trust.grade)}>{trust.grade.toLowerCase()}</Pill> : null}
                </div>
                {detailLoading || !trust ? (
                  <p className="air__load" role="status">Computing trust…</p>
                ) : (
                  <>
                    <div className="air__score">
                      <div className="air__scoren">{pct(trust.score)}</div>
                      <div className="air__bar">
                        <span className={`air__barfill air__barfill--${gradeTone(trust.grade)}`} style={{ width: pct(trust.score) }} />
                      </div>
                    </div>
                    {trust.blockers.length > 0 ? (
                      <ul className="air__blockers">
                        {trust.blockers.map((b, i) => (
                          <li key={i} className="air__blocker">{b}</li>
                        ))}
                      </ul>
                    ) : null}
                    <ul className="air__factors">
                      {trust.factors.map((f) => (
                        <li key={f.factor} className="air__factor">
                          <span className="air__fname">{f.factor.replace(/_/g, " ")}</span>
                          <span className="air__fbar">
                            <span
                              className="air__ffill"
                              style={{ width: f.maximum > 0 ? pct(f.score / f.maximum) : "0%" }}
                            />
                          </span>
                          <span className="air__fscore">
                            {f.score.toFixed(2)}/{f.maximum.toFixed(2)}
                          </span>
                          <span className="air__freason">{f.reason}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>

              <div className="air__card">
                <div className="air__cardhead">
                  <span className="air__csub">Remediations</span>
                </div>
                {remediations.length === 0 ? (
                  <Empty title="No open findings" hint="Findings raised by an assessment appear here to be worked or risk-accepted." />
                ) : (
                  <ul className="air__rems">
                    {remediations.map((r) => (
                      <li key={r.id} className="air__rem">
                        <div className="air__remtop">
                          <span className="air__remtitle">{r.title}</span>
                          <Pill tone={statusTone(r.status)}>{r.status.toLowerCase().replace(/_/g, " ")}</Pill>
                        </div>
                        <div className="air__remmeta">
                          <code>{r.finding_key}</code> · owner {r.owner_principal}
                          {r.resolved_by ? ` · resolved by ${r.resolved_by}` : ""}
                        </div>
                        {r.description ? <p className="air__remdesc">{r.description}</p> : null}
                        <label className="air__remact">
                          <span>Status</span>
                          <select
                            value={r.status}
                            disabled={savingId === r.id}
                            onChange={(e) =>
                              void changeStatus(r.id, e.target.value as (typeof REMEDIATION_STATES)[number])
                            }
                          >
                            {REMEDIATION_STATES.map((s) => (
                              <option key={s} value={s}>
                                {s.toLowerCase().replace(/_/g, " ")}
                              </option>
                            ))}
                          </select>
                        </label>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <details className="air__frameworks" onToggle={(e) => { if ((e.target as HTMLDetailsElement).open) void loadTemplates(); }}>
                <summary>Assessment frameworks</summary>
                <div className="air__fwinner">
                  {templates === null ? (
                    <p className="air__load" role="status">Loading frameworks…</p>
                  ) : templates.length === 0 ? (
                    <Empty title="No frameworks available" />
                  ) : (
                    <ul className="air__fwlist">
                      {templates.map((t) => (
                        <li key={t.template_key} className="air__fw">
                          <span className="air__fwtitle">{t.title}</span>
                          <span className="air__fwmeta">
                            {t.framework} {t.framework_version} · {t.controls.length} controls
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="air__fwnote">
                    An assessment against one of these frameworks seeds a control checklist; failing
                    a control opens a remediation above.
                  </p>
                </div>
              </details>

              <div className="air__footactions">
                <Button onClick={() => void loadDetail(selected.id)}>Refresh</Button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
