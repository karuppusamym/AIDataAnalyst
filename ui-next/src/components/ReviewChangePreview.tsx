import { useEffect, useState } from "react";
import { fetchGovernanceReviewDiff } from "../lib/api/governance";
import type { GovernanceReviewDiffRead } from "../lib/types";
import { Button } from "./primitives";

export function ReviewChangePreview({ reviewId, onReady }: {reviewId: string; onReady: (id: string | null) => void}) {
  const [data, setData] = useState<GovernanceReviewDiffRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(null); setPage(0); onReady(null);
    fetchGovernanceReviewDiff(reviewId, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (!value.diffable || value.review_id !== reviewId) throw new Error("A complete preview is unavailable for this review.");
      setData(value); onReady(reviewId);
    }).catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => { controller.abort(); };
  }, [reviewId, attempt, onReady]);
  if (error) return <div role="alert">{error}<Button onClick={() => setAttempt(attempt + 1)}>Retry review preview</Button></div>;
  if (!data) return <p role="status">Loading full review preview…</p>;
  const entries = data.entries ?? [];
  return <div>
    <p>{data.message}</p><p>{entries.length} field changes</p>
    {entries.slice(page * 50, (page + 1) * 50).map((entry, i) => <div className="prop__diff" key={i}>
      <strong>{entry.field}</strong><div>Before: {JSON.stringify(entry.before)}</div><div>Proposed: {JSON.stringify(entry.after)}</div>
    </div>)}
    {entries.length > 50 ? <nav aria-label="Review change pages"><Button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous changes</Button><Button disabled={(page + 1) * 50 >= entries.length} onClick={() => setPage(page + 1)}>Next changes</Button></nav> : null}
    <details><summary>Complete saved snapshots, including excluded rows</summary><pre>{JSON.stringify({before: data.before, after: data.after}, null, 2)}</pre></details>
  </div>;
}
