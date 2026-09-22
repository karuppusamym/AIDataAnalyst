import { useState } from "react";
import type {
  StudioChangeSetRead,
  StudioEvalMiningResult,
  StudioEvalQuestionRead,
  StudioEvalRunRead,
} from "../lib/types";
import { ApiError, fetchStudioEvalQuestions, fetchStudioEvalRun, mineStudioEvalQuestions } from "../lib/api";
import { readDecision, roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Dialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { FormError, FormSuccess, useAsyncResource, useSubmitAction } from "../components/screenState";
import { EvidenceList } from "./StudioVerify";
import { showStamp } from "./studioForm";
import { STUDIO_READ_ROLES, STUDIO_WRITE_ROLES, ITEMS_EDITABLE_STATUS, listOr } from "./studioRoles";

/* ---------------------------------------------------------------------------
   The eval-regression gate, seen (R11-AUD08): a change set's latest eval run, and
   the corpus of mined questions it runs against.

   WHAT AN EVAL QUESTION IS. Not free text and not a stored answer (ADR-0014 keeps
   raw values out of the control plane): one row saying "real usage exercised THIS
   governed metric or tool", with the id of the lineage edge that showed it.
   `POST /v1/studio/eval/mine` finds those rows -- consumption edges for tools, BI
   report edges for metrics -- and `POST .../test` re-checks every question for an
   object a change set touches: a question that used to resolve and no longer does
   fails the whole test run, and blocks submission, even when the item's own check
   passes.

   THE RUN EXISTS ONLY AFTER A TEST. `run_tests` is the only writer of eval runs
   and it moves the change set out of DRAFT first, so a DRAFT change set has none;
   asking for one would be a request that can only answer 404. It is not asked.
   For any other status the request is made, and a 404 is still the normal answer
   there ("no eval run recorded for this change set"), shown as the server's
   sentence in place of a run -- not as a failure to load.
--------------------------------------------------------------------------- */

type EvalLoad = { readonly run: StudioEvalRunRead | null; readonly none: string | null };

/** A change set's latest eval run, and each mined question's verdict in it. */
export function EvalRunSection({
  changeSet,
  refreshKey,
}: {
  changeSet: StudioChangeSetRead;
  /** Bumped by the screen after a test run, so the section re-reads what the run just wrote. */
  refreshKey: number;
}) {
  const session = useSession();
  // Same list as the change-set reads that put this pane on screen (matrix row for
  // `get_latest_eval_run`), so this is a second line of defence, not the first.
  const read = readDecision(session, STUDIO_READ_ROLES);
  const started = changeSet.status !== ITEMS_EDITABLE_STATUS;

  const state = useAsyncResource<EvalLoad>(
    (signal) =>
      fetchStudioEvalRun(changeSet.id, signal).then(
        (run) => ({ run, none: null }),
        (failure: unknown) => {
          // "No eval run recorded" is a state of the change set, not a fault.
          if (failure instanceof ApiError && failure.status === 404) return { run: null, none: failure.detail };
          throw failure;
        },
      ),
    [changeSet.id, changeSet.status, refreshKey],
    { enabled: started && read === "ask" },
  );

  return (
    <section className="cs__result" aria-label="Eval run">
      <div className="evp__sub">Eval regression gate</div>
      {!started ? (
        <p className="cs__none">No tests have been run on this change set, so there is no eval run yet.</p>
      ) : read === "skip" ? (
        <p className="cs__none">Not applicable to your roles: only sessions holding {listOr(STUDIO_READ_ROLES)} can read eval runs.</p>
      ) : state.error ? (
        <div className="cs__inlineerr" role="alert">
          <span>{state.error}</span>
          <Button onClick={state.reload}>Try again</Button>
        </div>
      ) : state.loading || !state.data ? (
        <div className="evp__load" role="status">Loading eval run…</div>
      ) : state.data.run ? (
        <EvalRunView run={state.data.run} />
      ) : (
        <p className="cs__none">{state.data.none}</p>
      )}
    </section>
  );
}

function EvalRunView({ run }: { run: StudioEvalRunRead }) {
  return (
    <>
      <div className="cs__result__head">
        <Pill tone={run.passed ? "ok" : "bad"}>{run.passed ? "passed" : "failed"}</Pill>
        <span className="cs__result__when">
          started {showStamp(run.started_at)} · completed {showStamp(run.completed_at)}
        </span>
      </div>
      <EvidenceList evidence={run.evidence} />
      {run.results.length === 0 ? (
        <p className="cs__none">
          No mined question covers the objects this change set touches, so the gate had nothing to check.
        </p>
      ) : (
        <ol className="evl">
          {run.results.map((result) => {
            const failures = Array.isArray(result.evidence.failures) ? (result.evidence.failures as unknown[]) : [];
            return (
              <li key={result.eval_question_id} className={`evi evi--${result.passed ? "ok" : "bad"}`}>
                <div className="evi__label">
                  {result.object_type} · {result.passed ? "passed" : "failed"}
                </div>
                <div className="evi__value">{result.label || result.object_id}</div>
                <div className="evi__source">{result.object_id}</div>
                {failures.length > 0 ? (
                  <ul className="cschk__errors">
                    {failures.map((failure, index) => (
                      <li key={index}>{String(failure)}</li>
                    ))}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
    </>
  );
}

const QUESTION_PAGE = 200;

/** What one mining pass reported, as a sentence and the counts the API returned. */
function MiningSummary({ result }: { result: StudioEvalMiningResult }) {
  return (
    <FormSuccess>
      Scanned {result.consumption_edges_scanned} consumption and {result.bi_edges_scanned} BI lineage edges: created{" "}
      {result.questions_created} question{result.questions_created === 1 ? "" : "s"}, {result.questions_already_mined}{" "}
      already mined.
      {result.truncated
        ? " The scan reached its limit, so older usage was not looked at; mining again does not go further back."
        : ""}
    </FormSuccess>
  );
}

/**
 * The mined question corpus, and -- for the write roles -- the action that grows it.
 *
 * MINING is organization-wide, idempotent (an object already mined is left alone, so
 * pressing it twice creates nothing the second time) and bounded (the API reports
 * `truncated` when a scan hit its limit). It writes only which governed object real
 * usage touched and the id of the edge that showed it -- never query text or results.
 */
export function EvalQuestionsDialog({ onClose }: { onClose: () => void }) {
  const session = useSession();
  const read = readDecision(session, STUDIO_READ_ROLES);
  // Mining writes the organization's question corpus (matrix row for `mine_eval_suite`).
  const mayMine = roleHolds(session.me?.roles, STUDIO_WRITE_ROLES);
  const [objectType, setObjectType] = useState<string>("");
  const mine = useSubmitAction<StudioEvalMiningResult>();

  const questions = useAsyncResource<StudioEvalQuestionRead[]>(
    (signal) => fetchStudioEvalQuestions({ objectType: objectType || null, limit: QUESTION_PAGE }, signal),
    [objectType],
    { enabled: read === "ask" },
  );

  const runMining = async () => {
    const result = await mine.run(() => mineStudioEvalQuestions());
    if (result) questions.reload();
  };

  const rows = questions.data ?? [];
  return (
    <Dialog
      title="Eval questions"
      description="Governed metrics and tools that real usage has exercised. Every test run re-checks the ones a change set touches."
      onClose={onClose}
      className="dlg--wide"
      footer={<Button onClick={onClose}>Close</Button>}
    >
      {mayMine ? (
        <div className="cs__mine">
          <p className="dlg__hint">
            Mining scans recent tool consumption and BI lineage edges and records one question for each governed metric
            or tool they touch that has none yet. It is safe to repeat: an object already mined is left alone. Only which
            object was used is stored, never a query or its result.
          </p>
          <Button variant="primary" disabled={mine.submitting} onClick={() => void runMining()}>
            {mine.submitting ? "Mining…" : "Mine eval questions"}
          </Button>
          {mine.error ? <FormError detail={mine.error} /> : null}
          {mine.result ? <MiningSummary result={mine.result} /> : null}
        </div>
      ) : session.me?.roles !== undefined ? (
        <p className="dlg__hint">Mining questions needs {listOr(STUDIO_WRITE_ROLES)}.</p>
      ) : null}

      {read === "skip" ? (
        <p className="cs__none">Not applicable to your roles: only sessions holding {listOr(STUDIO_READ_ROLES)} can read eval questions.</p>
      ) : (
        <>
          <Field label="Object type">
            <select value={objectType} onChange={(event) => setObjectType(event.target.value)}>
              <option value="">All types</option>
              <option value="METRIC">METRIC</option>
              <option value="TOOL">TOOL</option>
            </select>
          </Field>
          {questions.error ? (
            <ErrorState title="Eval questions could not be loaded" detail={questions.error} onRetry={questions.reload} />
          ) : questions.loading || !questions.data ? (
            <div className="evp__load" role="status">Loading eval questions…</div>
          ) : rows.length === 0 ? (
            <Empty
              title="No eval questions"
              hint={
                mayMine
                  ? "None have been mined yet. Mining looks for governed metrics and tools that usage has exercised."
                  : "None have been mined yet."
              }
            />
          ) : (
            <>
              <ol className="evl" aria-label="Eval questions">
                {rows.map((question) => (
                  <li key={question.id} className="evi evi--info">
                    <div className="evi__label">
                      {question.object_type} · {question.evidence_source.toLowerCase()}
                    </div>
                    <div className="evi__value">{question.label}</div>
                    <div className="evi__source">
                      {question.object_id} · mined {question.mined_at.slice(0, 10)}
                    </div>
                  </li>
                ))}
              </ol>
              {rows.length >= QUESTION_PAGE ? (
                <p className="cs__none">Showing the newest {QUESTION_PAGE}; older questions are not listed here.</p>
              ) : null}
            </>
          )}
        </>
      )}
    </Dialog>
  );
}
