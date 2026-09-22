import { useEffect, useId, useState } from "react";
import type { GovernanceReviewRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import {
  ApiError,
  LINK_PROPOSAL_CONFIDENCE_DEFAULT,
  LINK_PROPOSAL_CONFIDENCE_MAX,
  LINK_PROPOSAL_CONFIDENCE_MIN,
  LINK_PROPOSAL_LIMIT_DEFAULT,
  LINK_PROPOSAL_LIMIT_MAX,
  fetchGlossaryLinkProposals,
  generateGlossaryLinkProposals,
  submitGlossaryLinkProposal,
} from "../lib/api";
import type { GlossaryLinkProposalRead } from "../lib/api";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, ConfirmDialog, Dialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { FormError, useAsyncResource, useSubmitAction } from "../components/screenState";
import {
  GLOSSARY_REVIEW_WRITE_ROLES,
  Pager,
  ReviewHandoff,
  humanize,
  listOr,
  shortId,
  stamp,
  statusTone,
  useGlossaryReviewAccess,
} from "./glossaryReviewShared";
import "./GlossaryReview.css";

/* ---------------------------------------------------------------------------
   Glossary review -> Link proposals (R11-AUD08).

   A link proposal is the platform's suggestion that a table means what an
   approved glossary term says, with the evidence it is suggested on. Nothing is
   linked by suggesting it. The life of one is:

     DRAFT            -- `generate` created it, attributed to whoever ran it
                         (or to the steward agent, which proposes the same way
                         and submits as it goes, so its rows arrive already in
                         REVIEW_REQUIRED);
     REVIEW_REQUIRED  -- `submit` opened a `GLOSSARY_LINK_PROPOSAL` review
                         (`APPROVE_LINK`, tier T1);
     APPROVED         -- a different reviewer approved it, and only then does an
                         INFERRED `AssetTermLink` exist, carrying this
                         confidence and the annotation it came from;
     REJECTED         -- a reviewer declined it. The (table, term, annotation)
                         triple stays on file in every status, so the same match
                         is never proposed a second time.

   THE SEPARATION IS THE POINT. The steward who generates and submits is the
   maker; the review decision route refuses the maker as checker ("maker-checker
   separation is required"), so this tab says where a submission goes and who
   decides it instead of offering a way to approve one's own.

   EVIDENCE AND CONFIDENCE ARE SHOWN AS THE API RETURNS THEM. There is one
   matching strategy today (`APPROVED_LABEL_EXACT_MATCH`, in
   `glossary_link_candidates`) and its confidence takes two values, 1.00 and
   0.92 -- but the evidence object is free-form on the wire, so it is rendered
   as its own keys and values, with one plain sentence written only when the
   keys that sentence needs are all present.
--------------------------------------------------------------------------- */

const PAGE_SIZE = 25;

/** The values `GlossaryLinkProposal.status` takes -- see the lifecycle above. */
const PROPOSAL_STATUSES = ["DRAFT", "REVIEW_REQUIRED", "APPROVED", "REJECTED"] as const;

const isText = (value: unknown): value is string => typeof value === "string" && value.trim() !== "";
const capitalise = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

/** Which label of the TERM the annotation's label equalled
 *  (`glossary_link_candidates`: `DISPLAY_NAME`, `TERM_KEY`, `SYNONYM`). */
const TERM_LABEL_WORDS: Record<string, string> = {
  DISPLAY_NAME: "display name",
  TERM_KEY: "key",
  SYNONYM: "synonym",
};

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/** The evidence in a sentence, or null when it is not the shape that sentence
 *  needs. Saying less is the right failure: a sentence built from a guess is
 *  evidence that was not there. */
function evidenceSentence(proposal: GlossaryLinkProposalRead): string | null {
  const evidence = proposal.evidence;
  if (evidence.strategy !== "APPROVED_LABEL_EXACT_MATCH") return null;
  const kind = isText(evidence.term_label_kind) ? TERM_LABEL_WORDS[evidence.term_label_kind] : undefined;
  if (!isText(evidence.matched_label) || !kind) return null;
  const version = typeof evidence.annotation_version === "number" ? ` (version ${evidence.annotation_version})` : "";
  return (
    `An approved business annotation on ${proposal.table_name}${version} says “${evidence.matched_label}”, ` +
    `which equals the ${proposal.term_display_name} term's ${kind}.`
  );
}

const proposalTitle = (proposal: GlossaryLinkProposalRead): string =>
  `${proposal.table_name} → ${proposal.term_display_name}`;

function ProposalItem({
  proposal,
  mayWrite,
  mayOpenReviewQueue,
  onSubmit,
}: {
  proposal: GlossaryLinkProposalRead;
  mayWrite: boolean;
  mayOpenReviewQueue: boolean;
  onSubmit: (proposal: GlossaryLinkProposalRead) => void;
}) {
  const sentence = evidenceSentence(proposal);
  const entries = Object.entries(proposal.evidence);
  return (
    <li className="glrev__item">
      <div className="glrev__itemhead">
        <span className="glrev__title">{proposalTitle(proposal)}</span>
        <Pill tone={statusTone(proposal.status)}>{humanize(proposal.status)}</Pill>
        <Pill tone="mute">{`confidence ${proposal.confidence.toFixed(2)}`}</Pill>
      </div>
      <p className="glrev__meta">
        Proposed by {proposal.created_by} at {stamp(proposal.created_at)}
      </p>

      <div className="glrev__evidence" role="group" aria-label={`Evidence for ${proposalTitle(proposal)}`}>
        {sentence ? <p className="glrev__why">{sentence}</p> : null}
        {entries.length > 0 ? (
          <dl className="glrev__kv">
            {entries.map(([key, value]) => (
              <div key={key} className="glrev__kvrow">
                <dt>{capitalise(humanize(key))}</dt>
                <dd>{valueText(value)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="glrev__muted">The API returned no evidence for this proposal.</p>
        )}
      </div>

      <div className="glrev__links">
        <Button
          onClick={() => navigateTo("catalog", { asset: proposal.table_id })}
          title="Open this table in the Catalog"
        >
          {`Open table ${proposal.table_name}`}
        </Button>
        <Button
          onClick={() =>
            navigateTo("meaning", { view: "glossary", q: proposal.term_display_name })
          }
          title="Open this term in Business meaning"
        >
          {`Open term ${proposal.term_display_name}`}
        </Button>
      </div>

      {proposal.status === "REVIEW_REQUIRED" ? (
        <div className="glrev__hint">
          <p>
            Waiting for a decision in the Review queue
            {proposal.governance_review_id ? ` (review ${shortId(proposal.governance_review_id)})` : ""}. A
            different reviewer approves or rejects it; nobody can decide a review they opened.
          </p>
          {mayOpenReviewQueue && proposal.governance_review_id ? (
            <Button onClick={() => navigateTo("governance", { review: proposal.governance_review_id as string })}>
              Open the review
            </Button>
          ) : null}
        </div>
      ) : null}
      {proposal.status === "APPROVED" ? (
        <p className="glrev__hint">
          Approved{proposal.reviewed_by ? ` by ${proposal.reviewed_by}` : ""} at {stamp(proposal.reviewed_at)}. The
          table and the term are linked (an inferred link, at confidence {proposal.confidence.toFixed(2)}).
        </p>
      ) : null}
      {proposal.status === "REJECTED" ? (
        <p className="glrev__hint">
          Rejected{proposal.reviewed_by ? ` by ${proposal.reviewed_by}` : ""} at {stamp(proposal.reviewed_at)}. No
          link was made, and this match is not proposed again.
        </p>
      ) : null}

      {proposal.status === "DRAFT" && mayWrite ? (
        <div className="glrev__actions">
          <Button variant="primary" onClick={() => onSubmit(proposal)}>
            {`Submit ${proposalTitle(proposal)} for review`}
          </Button>
        </div>
      ) : null}
    </li>
  );
}

/** `generate` with the two bounds the API takes, its effect stated first. */
function GenerateDialog({
  organizationId,
  onClose,
  onGenerated,
}: {
  organizationId: string;
  onClose: () => void;
  onGenerated: (result: PageOf<GlossaryLinkProposalRead>, requestedLimit: number) => void;
}) {
  const [confidence, setConfidence] = useState(String(LINK_PROPOSAL_CONFIDENCE_DEFAULT));
  const [limit, setLimit] = useState(String(LINK_PROPOSAL_LIMIT_DEFAULT));
  const submit = useSubmitAction<PageOf<GlossaryLinkProposalRead>>();
  const confidenceHintId = useId();
  const limitHintId = useId();

  const confidenceValue = Number(confidence);
  const limitValue = Number(limit);
  const confidenceOk =
    confidence.trim() !== "" &&
    Number.isFinite(confidenceValue) &&
    confidenceValue >= LINK_PROPOSAL_CONFIDENCE_MIN &&
    confidenceValue <= LINK_PROPOSAL_CONFIDENCE_MAX;
  const limitOk =
    limit.trim() !== "" && Number.isInteger(limitValue) && limitValue >= 1 && limitValue <= LINK_PROPOSAL_LIMIT_MAX;

  const confirm = async () => {
    if (!confidenceOk || !limitOk) return;
    const result = await submit.run(() =>
      generateGlossaryLinkProposals(organizationId, { minimum_confidence: confidenceValue, limit: limitValue }),
    );
    if (result !== null) onGenerated(result, limitValue);
  };

  return (
    <Dialog
      title="Generate link proposals?"
      description={
        "Matches each approved business annotation's name and synonyms, exactly and ignoring case, against " +
        "approved, active terms' display names, keys and synonyms, and creates a DRAFT proposal for each pair " +
        "that is not already linked or proposed before, in any status (so a rejected link is not proposed " +
        "again). The proposals are attributed to you. Nothing is linked: each must be submitted for review, " +
        "and a different reviewer must approve it."
      }
      onClose={onClose}
      dismissOnBackdrop={false}
      footer={
        <>
          <Button onClick={onClose} disabled={submit.submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!confidenceOk || !limitOk || submit.submitting}
            onClick={() => void confirm()}
          >
            {submit.submitting ? "Working…" : "Generate proposals"}
          </Button>
        </>
      }
    >
      <Field label="Minimum confidence">
        <input
          type="number"
          inputMode="decimal"
          step="0.01"
          min={LINK_PROPOSAL_CONFIDENCE_MIN}
          max={LINK_PROPOSAL_CONFIDENCE_MAX}
          value={confidence}
          aria-invalid={!confidenceOk}
          aria-describedby={confidenceHintId}
          onChange={(event) => setConfidence(event.target.value)}
        />
      </Field>
      <span className="dlg__hint" id={confidenceHintId}>
        Between {LINK_PROPOSAL_CONFIDENCE_MIN.toFixed(2)} and {LINK_PROPOSAL_CONFIDENCE_MAX.toFixed(2)}. Confidence is
        1.00 when the annotation&rsquo;s business name equals the term&rsquo;s display name and 0.92 for every other
        exact match, so a minimum above 0.92 keeps only display-name matches.
      </span>
      <Field label="Most proposals to create">
        <input
          type="number"
          inputMode="numeric"
          step="1"
          min={1}
          max={LINK_PROPOSAL_LIMIT_MAX}
          value={limit}
          aria-invalid={!limitOk}
          aria-describedby={limitHintId}
          onChange={(event) => setLimit(event.target.value)}
        />
      </Field>
      <span className="dlg__hint" id={limitHintId}>
        A whole number from 1 to {LINK_PROPOSAL_LIMIT_MAX}. A run that reaches it stops; run it again for more.
      </span>
      {submit.error ? <FormError detail={submit.error} /> : null}
    </Dialog>
  );
}

interface Notice {
  text: string;
  /** Present when the notice is a hand-off to the Review queue: the review that was opened. */
  reviewId?: string;
}

export function GlossaryLinkProposals() {
  const organizationId = useOrgId();
  const access = useGlossaryReviewAccess();
  const [params, setParams] = useUrlState();
  const statusParam = params.get("status") ?? "";
  const status = (PROPOSAL_STATUSES as readonly string[]).includes(statusParam) ? statusParam : "";
  const [offset, setOffset] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [generating, setGenerating] = useState(false);
  const [submitting, setSubmitting] = useState<GlossaryLinkProposalRead | null>(null);
  const submit = useSubmitAction<GovernanceReviewRead>();

  const list = useAsyncResource<PageOf<GlossaryLinkProposalRead>>(
    (signal) =>
      fetchGlossaryLinkProposals(organizationId, { status: status || null, limit: PAGE_SIZE, offset }, signal),
    [organizationId, status, offset],
    { enabled: access.read === "ask" },
  );
  const reload = list.reload;

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  // A page past the end (the last DRAFT on page 2 was just submitted away from a
  // DRAFT filter) is not "no proposals": step back to the last page that exists.
  useEffect(() => {
    if (list.data && list.data.items.length === 0 && offset > 0 && list.data.total > 0) {
      setOffset(Math.max(0, (Math.ceil(list.data.total / PAGE_SIZE) - 1) * PAGE_SIZE));
    }
  }, [list.data, offset]);

  const setStatus = (next: string) => {
    setOffset(0);
    setParams({ status: next || null });
  };

  const openSubmit = (proposal: GlossaryLinkProposalRead) => {
    submit.reset();
    setSubmitting(proposal);
  };
  const runSubmit = async () => {
    if (!submitting) return;
    const proposal = submitting;
    const review = await submit.run(async () => {
      try {
        return await submitGlossaryLinkProposal(proposal.id);
      } catch (failure) {
        // 409 "only draft link proposals can be submitted": somebody submitted it
        // first, so the row on screen is stale and the list is re-read.
        if (failure instanceof ApiError && failure.status === 409) reload();
        throw failure;
      }
    });
    if (review === null) return; // the refusal stays in the dialog, in the server's words
    setSubmitting(null);
    setNotice({
      text:
        `${proposalTitle(proposal)} was submitted. A governance review is waiting in the Review queue; ` +
        "someone other than you approves or rejects it. Approving links the table to the term; rejecting " +
        "closes the proposal for good.",
      reviewId: review.id,
    });
    reload();
  };

  return (
    <div className="glrev__view">
      <div className="glrev__head">
        <div>
          <h2 className="glrev__h2">Term-link proposals</h2>
          <p className="glrev__lede">
            Suggested links between tables and approved glossary terms, each with the evidence it rests on. A link
            exists only once a different reviewer approves the proposal.
          </p>
        </div>
        {access.mayWrite ? (
          <div className="glrev__headactions">
            <Button variant="primary" onClick={() => setGenerating(true)}>
              Generate proposals
            </Button>
          </div>
        ) : null}
      </div>

      {access.identityKnown && !access.mayWrite ? (
        <p className="glrev__hint">
          You can read proposals. Generating and submitting them is for sessions holding{" "}
          {listOr(GLOSSARY_REVIEW_WRITE_ROLES)}.
        </p>
      ) : null}

      {notice ? (
        notice.reviewId ? (
          <ReviewHandoff reviewId={notice.reviewId} mayOpenReviewQueue={access.mayOpenReviewQueue}>
            {notice.text}
          </ReviewHandoff>
        ) : (
          <p className="glrev__notice" role="status">
            {notice.text}
          </p>
        )
      ) : null}

      {access.read === "skip" ? (
        <p className="glrev__hint">
          Not applicable to your roles: only sessions holding one of the roles that read glossary review can see
          link proposals.
        </p>
      ) : (
        <>
          <div className="glrev__filters">
            <Field label="Status">
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">All statuses</option>
                {PROPOSAL_STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {list.error ? (
            <ErrorState title="Link proposals could not be loaded" detail={list.error} onRetry={reload} />
          ) : access.read === "wait" || !list.data ? (
            <div className="glrev__loading" role="status">
              Loading link proposals…
            </div>
          ) : items.length === 0 && total === 0 ? (
            <Empty
              title={status ? `No proposals with status ${humanize(status)}` : "No link proposals"}
              hint={
                status
                  ? "Choose another status, or all statuses, to see the rest."
                  : access.mayWrite
                    ? "Generate proposals to look for tables whose approved business names match an approved glossary term."
                    : "No link proposal has been generated in this organization."
              }
            />
          ) : (
            <>
              <ul className="glrev__list" aria-label="Term-link proposals">
                {items.map((proposal) => (
                  <ProposalItem
                    key={proposal.id}
                    proposal={proposal}
                    mayWrite={access.mayWrite}
                    mayOpenReviewQueue={access.mayOpenReviewQueue}
                    onSubmit={openSubmit}
                  />
                ))}
              </ul>
              <Pager
                offset={offset}
                limit={PAGE_SIZE}
                total={total}
                shown={items.length}
                noun="proposals"
                onPage={setOffset}
              />
            </>
          )}
        </>
      )}

      {generating ? (
        <GenerateDialog
          organizationId={organizationId}
          onClose={() => setGenerating(false)}
          onGenerated={(result, requestedLimit) => {
            setGenerating(false);
            const created = result.items.length;
            setNotice({
              text:
                created === 0
                  ? "Generation found no new matches: every match is already linked or was proposed before, or none reaches the minimum confidence."
                  : `Generated ${created} draft proposal${created === 1 ? "" : "s"}. Nothing is linked yet: each must be submitted and approved by a different reviewer.` +
                    (created >= requestedLimit
                      ? ` It stopped at its limit of ${requestedLimit}: run it again for more.`
                      : ""),
            });
            reload();
          }}
        />
      ) : null}

      {submitting ? (
        <ConfirmDialog
          title="Submit this proposal for review?"
          description={
            `${proposalTitle(submitting)}: this moves the proposal from draft to review and opens a governance ` +
            "review in the Review queue. A different reviewer approves or rejects it, and you cannot decide " +
            `your own submission. Approving links the table to the term (an inferred link, at confidence ` +
            `${submitting.confidence.toFixed(2)}); rejecting closes the proposal, and the same match is not ` +
            "proposed again."
          }
          confirmLabel="Submit for review"
          busy={submit.submitting}
          error={submit.error}
          onConfirm={() => void runSubmit()}
          onCancel={() => setSubmitting(null)}
        />
      ) : null}
    </div>
  );
}
