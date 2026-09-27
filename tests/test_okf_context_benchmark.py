"""Tests for the OKF context retrieval benchmark (tracker R11-OKF02, R11-FP13).

`scripts/okf_context_benchmark.py` measures **retrieval only, not answer quality**: given a
question, does the stored knowledge bundle put the right object in front of the model. These
tests establish that the harness measures what it claims to, and pin what it measured:

  1. It is deterministic: two runs over the committed corpus are byte-identical.
  2. It is the real procedure: for every case its `full` variant is exactly
     `aida.okf_context.select_context`, so it cannot drift into scoring a copy of the ranking.
  3. The ablations only remove what the ranker can see. No ablation beats the full ranking on the
     committed corpus, each signal it removes costs something, and the documents handed out stay
     the full bundle's.
  4. It refuses what it cannot score: a question with no expected object, a reused case that
     does not exist, an object the estate does not hold.
  5. It makes no network call and loads no provider or embedding module, and has no `--live`.
  6. The API mode, driven by a fake transport that serves the *real* pipeline in the real
     response shape, reads only, skips what an estate cannot answer, and reproduces the in-process
     result -- so the two modes agree on the same bundle.
  7. The numbers in `Docs/90-reference/okf-context-retrieval-benchmark.md` are the ones pinned
     here. If the ranking or the fixture moves, this file goes red on purpose: re-run the harness
     and update that document in the same change.

**None of this measures answer quality.** The corpus and estate overlay were authored by the same
session that read the ranking code; see the corpus file's own description.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aida.okf_context import (
    DEFAULT_MAX_CHARS,
    STATUS_MATCHED,
    OkfContext,
    citation_ids,
    parse_document,
    render_markdown,
    select_context,
    terms,
)
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    TYPE_COLUMN_SET,
    TYPE_CONCEPT,
    TYPE_MATERIALIZED_VIEW,
    TYPE_ROUTINE,
    TYPE_SCHEMA,
    TYPE_SOURCE,
    TYPE_TABLE,
    TYPE_TOOL_VERSION,
    TYPE_VIEW,
    document_subjects,
    export_okf_bundle,
)
from aida.schemas import (
    OkfBundleRead,
    OkfChangeSummaryRead,
    OkfContextDocumentRead,
    OkfContextOmissionRead,
    OkfContextRead,
    OkfContextSectionRead,
    OkfPublicationRead,
)
from scripts.okf_context_benchmark import (
    CORPUS_DIR,
    REPO_ROOT,
    VARIANTS,
    ApiError,
    Case,
    Corpus,
    CorpusError,
    DeliveredDocument,
    FixtureBundle,
    ObjectRef,
    Report,
    Selection,
    ablate,
    api_headers,
    build_fixture_snapshot,
    discover_target,
    identify,
    load_corpus,
    main,
    observed_status,
    parse_args,
    render_fixture,
    run_api,
    run_in_process,
    score_case,
    select_with_ranking,
    selection_from_context,
    summarise,
)

ORG = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87"
DATASOURCE = "ddb57d0b-0000-4000-8000-000000000001"
VERSION = "0a1b2c3d-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture(scope="module")
def bundle(corpus: Corpus) -> FixtureBundle:
    return render_fixture(corpus.estate)


@pytest.fixture(scope="module")
def report(corpus: Corpus) -> Report:
    return run_in_process(corpus, budgets=(DEFAULT_MAX_CHARS, 2_000))


_FIXTURE_ONLY = {
    "description-branch-offices",
    "description-declined",
    "description-serious",
    "column-date-of-birth",
    "ambiguity-email-address",
    "tool-account-summary",
}


def _summary(report: Report, variant: str, budget: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    return summarise(report.variants[variant][budget])


# --- 1. determinism ----------------------------------------------------------------------------


def test_two_runs_over_the_committed_corpus_are_byte_identical(
    corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    first = json.dumps(run_in_process(corpus).to_json(), sort_keys=True)
    second = json.dumps(run_in_process(corpus).to_json(), sort_keys=True)
    assert first == second
    assert main(["--format", "json"]) == 0
    printed_once = capsys.readouterr().out
    assert main(["--format", "json"]) == 0
    assert capsys.readouterr().out == printed_once
    # No clock anywhere in the report: a timestamp would make every run differ.
    assert not any(word in printed_once.lower() for word in ("timestamp", "generated_at"))


# --- 2. it is the real procedure ---------------------------------------------------------------


def test_the_full_variant_is_exactly_the_real_select_context(
    corpus: Corpus, bundle: FixtureBundle
) -> None:
    for case in corpus.cases:
        ours = select_with_ranking(
            bundle.snapshot, bundle.documents, case.question, max_chars=DEFAULT_MAX_CHARS
        )
        real = select_context(
            bundle.snapshot, bundle.documents, case.question, max_chars=DEFAULT_MAX_CHARS
        )
        assert ours == real, case.id


def test_the_fixture_bundle_is_the_one_the_numbers_were_measured_on(
    bundle: FixtureBundle,
) -> None:
    """Pinned so a change to the estate constants or the overlay is visible, not silent."""
    assert dict(bundle.facts) == {
        "documents": 21,
        "objects": 10,
        "objects_with_approved_description": 7,
        "routines": 2,
        "concepts": 1,
        "tools": 1,
        "wide_objects": 1,
    }


# --- 3. ablations ------------------------------------------------------------------------------


def test_no_ablation_beats_the_full_ranking_and_each_signal_costs_something(
    report: Report,
) -> None:
    for budget in report.budgets:
        full = _summary(report, "full", budget)
        for variant in ("no_descriptions", "no_aliases", "names_only"):
            ablated = _summary(report, variant, budget)
            for metric in ("hit_at_1", "hit_at_3", "delivered"):
                assert full[metric]["n"] >= ablated[metric]["n"], (variant, metric, budget)
            assert full["mrr"] >= ablated["mrr"], (variant, budget)
    full = _summary(report, "full")
    no_descriptions = _summary(report, "no_descriptions")
    no_aliases = _summary(report, "no_aliases")
    names_only = _summary(report, "names_only")
    # Each single ablation loses something on this corpus, and removing everything loses at
    # least as much as removing either signal alone.
    assert no_descriptions["hit_at_1"]["n"] < full["hit_at_1"]["n"]
    assert no_aliases["hit_at_1"]["n"] < full["hit_at_1"]["n"]
    assert names_only["hit_at_1"]["n"] <= min(
        no_descriptions["hit_at_1"]["n"], no_aliases["hit_at_1"]["n"]
    )


def test_ablation_changes_what_is_ranked_and_not_what_is_handed_out(
    corpus: Corpus, bundle: FixtureBundle
) -> None:
    before = bundle.snapshot.content_digest()
    names_only = ablate(bundle.snapshot, "names_only")
    assert names_only is not bundle.snapshot
    assert bundle.snapshot.content_digest() == before  # a copy: the original is untouched
    assert all(obj.description.state != DESCRIPTION_APPROVED for obj in names_only.objects)
    assert all(not obj.columns for obj in names_only.objects)
    assert all(not concept.aliases and not concept.definition for concept in names_only.concepts)
    assert ablate(bundle.snapshot, "full") is bundle.snapshot
    with pytest.raises(ValueError, match="unknown variant"):
        ablate(bundle.snapshot, "made_up")
    for case in corpus.cases:
        for variant in VARIANTS:
            context = select_with_ranking(
                ablate(bundle.snapshot, variant),
                bundle.documents,
                case.question,
                max_chars=DEFAULT_MAX_CHARS,
            )
            for document in context.documents:  # the bundle's own bytes, whatever the ranker saw
                assert document.sha256 == bundle.documents[document.path][1], (case.id, variant)


# --- 7. what was measured (pinned; the results document quotes these) --------------------------


def test_the_measured_numbers_are_the_ones_the_results_document_reports(report: Report) -> None:
    expected = {
        # variant: (hit@1, hit@3, delivered) of 24 cases that expect an object
        "full": (19, 22, 22),
        "no_descriptions": (16, 19, 19),
        "no_aliases": (18, 21, 21),
        "names_only": (13, 15, 16),
    }
    measured = {
        variant: tuple(
            _summary(report, variant)[key]["n"] for key in ("hit_at_1", "hit_at_3", "delivered")
        )
        for variant in expected
    }
    hint = (
        "The ranking or the fixture moved. If that was deliberate, re-run "
        "`python scripts/okf_context_benchmark.py --format markdown` and update "
        "Docs/90-reference/okf-context-retrieval-benchmark.md in the same change."
    )
    assert measured == expected, hint
    assert _summary(report, "full")["cases_expecting_objects"] == 24
    full = _summary(report, "full")
    assert full["reached"] == {"direct": 19, "via_link": 3, "missed": 2}, hint
    # The refusals and false matches, by name: they are the findings.
    assert full["refused_when_answerable"] == ["paraphrase-payroll", "paraphrase-suspicious"], hint
    assert full["false_matches"] == ["no-match-adjacent-satisfaction"], hint
    assert full["no_match_correct"]["n"] == 3 and full["no_match_correct"]["of"] == 4, hint
    assert full["false_ambiguity"]["n"] == 0 and full["false_ambiguity"]["of"] == 18, hint
    assert full["correct_ambiguity"]["n"] == 1 and full["correct_ambiguity"]["of"] == 1, hint
    assert full["gap_preserved"]["n"] == 2 and full["gap_preserved"]["of"] == 2, hint
    assert full["evidence_text_delivered"]["n"] == 1, hint
    assert _summary(report, "names_only")["evidence_text_delivered"]["n"] == 0, hint
    assert _summary(report, "names_only")["correct_ambiguity"]["n"] == 0, hint


def test_a_tight_budget_cuts_sections_before_it_loses_objects(report: Report) -> None:
    roomy = _summary(report, "full", DEFAULT_MAX_CHARS)
    tight = _summary(report, "full", 2_000)
    assert roomy["chars"]["cases_with_sections_left_out"] == 0
    assert tight["chars"]["cases_with_sections_left_out"] > 0
    assert tight["chars"]["mean_used"] < roomy["chars"]["mean_used"]
    assert tight["delivered"]["n"] == roomy["delivered"]["n"]


def test_the_status_split_names_ambiguity_and_scores_it_against_the_corpus(
    report: Report,
) -> None:
    results = {item.case.id: item for item in report.variants["full"][DEFAULT_MAX_CHARS]}
    tied = results["ambiguity-email-address"]
    assert tied.observed_status == "AMBIGUOUS" and tied.case.expect_ambiguous
    assert tied.rank == 1  # an expected table is among the tied pair, delivered first
    # Every AMBIGUOUS answer is one of the corpus's tied cases; none is a false ambiguity.
    ambiguous = [item.case.id for item in results.values() if item.observed_status == "AMBIGUOUS"]
    assert ambiguous == ["ambiguity-email-address"]
    # observed_status is the API's MATCHED plus a non-empty `ambiguous` list, and nothing else.
    assert observed_status(STATUS_MATCHED, ["a", "b"]) == "AMBIGUOUS"
    assert observed_status(STATUS_MATCHED, []) == STATUS_MATCHED
    assert observed_status("NO_MATCH", ["a"]) == "NO_MATCH"


# --- scoring, one way for a case to be wrong at a time -----------------------------------------


def test_identify_reads_an_object_from_a_document_type_and_title_alone() -> None:
    """Both modes identify a delivered document by these two fields and nothing else."""
    table = ObjectRef.of("TABLE", "fact_account_balances")
    assert identify(TYPE_TABLE, "warehouse.public.fact_account_balances") == table
    assert identify(TYPE_VIEW, "a.b.fact_account_balances") == table
    assert identify(TYPE_MATERIALIZED_VIEW, "a.b.fact_account_balances") == table
    # A column set is its parent object, so it neither adds a subject nor hides its parent.
    assert identify(TYPE_COLUMN_SET, "a.b.fact_account_balances: columns 101-150") == table
    assert identify(TYPE_ROUTINE, "a.b.nightly_settlement_rollup") == ObjectRef.of(
        "ROUTINE", "nightly_settlement_rollup"
    )
    assert identify(TYPE_CONCEPT, "End of day position") == ObjectRef.of(
        "ONTOLOGY_CONCEPT", "end_of_day_position"
    )
    assert identify(TYPE_TOOL_VERSION, "Customer Account Summary (version 3)") == ObjectRef.of(
        "GOVERNED_TOOL", "customer-account-summary"
    )
    assert identify(TYPE_SOURCE, "x") is None and identify(TYPE_SCHEMA, "x") is None


def _doc(kind: str, name: str, hop: int = 0) -> DeliveredDocument:
    return DeliveredDocument(f"{name}.md", kind, name, hop, 0.0, identify(kind, name))


def _selection(*docs: DeliveredDocument, text: str = "", status: str = "MATCHED") -> Selection:
    return Selection(
        status=status,
        documents=docs,
        ambiguous=(),
        max_chars=16_000,
        used_chars=len(text),
        omitted_count=0,
        text=" ".join(text.split()).lower(),
    )


def _case(*expected: str, forbidden: tuple[str, ...] = (), **fields: Any) -> Case:
    return Case(
        id="c",
        category="x",
        question="q",
        expected_status=fields.pop("expected_status", "MATCHED"),
        expected=tuple(ObjectRef.of("TABLE", name) for name in expected),
        forbidden=tuple(ObjectRef.of("TABLE", name) for name in forbidden),
        **fields,
    )


def test_rank_counts_distinct_subjects_and_a_linked_object_is_labelled_as_linked() -> None:
    a, b, c = (_doc(TYPE_TABLE, f"w.p.{name}") for name in ("a", "b", "c"))
    linked = _doc(TYPE_TABLE, "w.p.linked", hop=1)
    # b is delivered with a column set between a and c; the set is b, not a new subject.
    sets = _doc(TYPE_COLUMN_SET, "w.p.b: columns 1-100")
    selection = _selection(a, b, sets, c, linked)
    assert (score_case(_case("b"), selection).rank, score_case(_case("b"), selection).reached) == (
        2,
        "direct",
    )
    assert score_case(_case("c"), selection).rank == 3  # not 4: the column set added no subject
    via = score_case(_case("linked"), selection)
    assert (via.rank, via.reached) == (4, "via_link")
    missed = score_case(_case("nowhere"), selection)
    assert (missed.rank, missed.reached, missed.complete) == (None, "missed", False)
    # Any-of for the hit, all-of for completeness.
    both = score_case(_case("a", "linked"), selection)
    assert both.rank == 1 and both.complete
    partial = score_case(_case("a", "nowhere"), selection)
    assert partial.rank == 1 and not partial.complete


def test_a_forbidden_object_that_is_delivered_is_reported_and_one_that_is_not_is_not() -> None:
    selection = _selection(_doc(TYPE_TABLE, "w.p.a"), _doc(TYPE_TABLE, "w.p.secret"))
    leaked = score_case(_case("a", forbidden=("secret",)), selection)
    assert leaked.forbidden_delivered == ("TABLE:secret",)
    kept = score_case(_case("a", forbidden=("elsewhere",)), selection)
    assert kept.forbidden_delivered == ()
    assert summarise([leaked, kept])["gap_preserved"] == {"n": 1, "of": 2, "rate": 0.5}


def test_evidence_text_is_present_only_when_every_phrase_is_delivered() -> None:
    doc = _doc(TYPE_TABLE, "w.p.a")
    text = "The customer's   date of   birth.   More"
    assert (
        score_case(
            _case("a", expected_text=("The customer's date of birth.",)), _selection(doc, text=text)
        ).text_present
        is True
    )
    assert (
        score_case(
            _case("a", expected_text=("date of birth", "email")), _selection(doc, text=text)
        ).text_present
        is False
    )
    assert score_case(_case("a"), _selection(doc, text=text)).text_present is None


def test_a_no_match_case_is_correct_only_when_nothing_matched_and_never_has_a_rank() -> None:
    silent = _case(expected_status="NO_MATCH")
    refused = score_case(silent, _selection(status="NO_MATCH"))
    matched = score_case(silent, _selection(_doc(TYPE_TABLE, "w.p.a")))
    assert (refused.reached, refused.rank, matched.reached, matched.rank) == (
        "n/a",
        None,
        "n/a",
        None,
    )
    summary = summarise([refused, matched])
    assert summary["no_match_correct"] == {"n": 1, "of": 2, "rate": 0.5}
    assert summary["false_matches"] == ["c"]


# --- 4. what it refuses ------------------------------------------------------------------------


def _corpus_dir(tmp_path: Path, cases: list[dict[str, Any]]) -> Path:
    for name in ("footprint_enrichment_corpus.json", "answer_evaluation_corpus.json"):
        shutil.copy(CORPUS_DIR / name, tmp_path / name)
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps({"description": "t", "estate": {}, "cases": cases}), encoding="utf-8"
    )
    return path


_OK = {"id": "x", "category": "c", "question": "loan applications"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_OK, "no expected object"),
        ({**_OK, "expected": []}, "no expected object"),
        (
            {
                "reuse": {
                    "corpus": "answer_evaluation_corpus.json",
                    "id": "refusal-masking-bypass-over-enriched-context",
                },
                "category": "refusal",
            },
            "no expected object",
        ),
        (
            {
                "reuse": {
                    "corpus": "footprint_enrichment_corpus.json",
                    "id": "gap-proposed-lineage-steers-nothing",
                },
                "category": "gap",
            },
            "no expected object",
        ),
        (
            {
                **_OK,
                "expected_status": "NO_MATCH",
                "expected": [{"object_type": "TABLE", "object_key": "a"}],
            },
            "NO_MATCH but expected objects",
        ),
        (
            {**_OK, "expected_status": "MATCHED"},
            "MATCHED but no expected object",
        ),
        (
            {
                **_OK,
                "expected": [{"object_type": "TABLE", "object_key": "a"}],
                "exactly_one_answer": True,
                "expect_ambiguous": True,
            },
            "contradict",
        ),
        (
            {
                "reuse": {"corpus": "footprint_enrichment_corpus.json", "id": "no-such-case"},
                "category": "c",
            },
            "has no case",
        ),
        (
            {**_OK, "expected": [{"object_type": "VIEW_OF_THE_MOON", "object_key": "a"}]},
            "unsupported object_type",
        ),
        ({**_OK, "question": "  ", "expected_status": "NO_MATCH"}, "question is empty"),
        ({"id": "x", "question": "q", "expected_status": "NO_MATCH"}, "no category"),
    ],
)
def test_the_loader_refuses_a_case_it_cannot_score(
    tmp_path: Path, entry: dict[str, Any], message: str
) -> None:
    with pytest.raises(CorpusError, match=message):
        load_corpus(_corpus_dir(tmp_path, [entry]))


def test_an_explicit_no_match_is_accepted_and_a_duplicate_id_is_not(tmp_path: Path) -> None:
    silent = {**_OK, "expected_status": "NO_MATCH"}
    assert load_corpus(_corpus_dir(tmp_path, [silent])).cases[0].expected == ()
    with pytest.raises(CorpusError, match="duplicate case id"):
        load_corpus(_corpus_dir(tmp_path, [silent, silent]))


def test_an_object_the_estate_does_not_hold_is_refused_not_scored_as_a_miss(
    tmp_path: Path, corpus: Corpus
) -> None:
    ghost = {**_OK, "expected": [{"object_type": "TABLE", "object_key": "dim_ghost"}]}
    with pytest.raises(CorpusError, match="does not hold.*dim_ghost"):
        run_in_process(_with_estate(tmp_path, corpus, [ghost]))


def _with_estate(tmp_path: Path, corpus: Corpus, cases: list[dict[str, Any]]) -> Corpus:
    path = _corpus_dir(tmp_path, cases)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["estate"] = dict(corpus.estate)
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_corpus(path)


def test_reused_cases_take_their_question_and_object_from_the_other_corpora(
    corpus: Corpus,
) -> None:
    footprint = json.loads((CORPUS_DIR / "footprint_enrichment_corpus.json").read_text("utf-8"))
    answers = json.loads((CORPUS_DIR / "answer_evaluation_corpus.json").read_text("utf-8"))
    questions = {
        f"{name}#{case['id']}": case["question"]
        for name, data in (
            ("footprint_enrichment_corpus.json", footprint),
            ("answer_evaluation_corpus.json", answers),
        )
        for case in data["cases"]
    }
    reused = [case for case in corpus.cases if case.provenance != "own"]
    assert len(reused) == 11
    for case in reused:
        assert case.question == questions[case.provenance], case.id
    by_id = {case.id: case for case in corpus.cases}
    assert by_id["concept-found-by-alias"].expected == (
        ObjectRef.of("ONTOLOGY_CONCEPT", "end_of_day_position"),
    )
    # The footprint corpus's gap becomes a forbidden object here, with a stated expected one.
    gap = by_id["gap-proposed-lineage-steers-nothing"]
    assert gap.forbidden == (ObjectRef.of("TABLE", "fact_fraud_alerts"),)
    assert gap.expected == (ObjectRef.of("ROUTINE", "quarterly_fee_accrual"),)


def test_no_overlay_text_recreates_a_lexical_path_to_a_calibrated_target(
    corpus: Corpus, bundle: FixtureBundle
) -> None:
    """The footprint corpus's calibration is that no question shares a word with its target
    table, so the target is reachable only through the enrichment. The overlay adds descriptions
    and columns that corpus never had; this proves it added no such word (an earlier calibration
    leak, `received`, is recorded in the tracker). The lexical control is exempt by design."""
    by_name = {obj.name: obj for obj in bundle.snapshot.objects}
    checked = 0
    for case in corpus.cases:
        if case.provenance == "own" or case.id == "lexical-control-answers-without-enrichment":
            continue
        for ref in (*case.expected, *case.forbidden):
            if ref.object_type != "TABLE":
                continue
            obj = by_name[ref.object_key]
            words = set(terms(obj.name)) | set(terms(obj.description.text))
            for column in obj.columns:
                words |= set(terms(column.name)) | set(terms(column.description.text))
            shared = set(terms(case.question)) & words
            assert not shared, (case.id, ref.label, sorted(shared))
            checked += 1
    assert checked >= 5


# --- 5. no network, no provider ----------------------------------------------------------------

_PROBE = """
import contextlib, io, json, socket, sys

def _blocked(*args, **kwargs):
    raise RuntimeError("network attempted")

socket.socket.connect = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
sys.path.insert(0, ".")
from scripts import okf_context_benchmark as m

buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    code = m.main(["--format", "json"])
watch = ("openai", "anthropic", "google", "httpx", "requests", "aiohttp",
         "aida.model_gateway", "aida.embedding_provider", "aida.db", "aida.config", "fastapi")
loaded = sorted({n for n in sys.modules if any(n == w or n.startswith(w + ".") for w in watch)})
print(json.dumps({"exit": code, "loaded": loaded, "bytes": len(buffer.getvalue())}))
"""


def test_the_default_mode_makes_no_network_call_and_loads_no_provider() -> None:
    env = {name: value for name, value in os.environ.items() if name != "AIDA_ENVIRONMENT"}
    done = subprocess.run(  # noqa: S603 -- our own interpreter, our own fixed probe
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
    outcome = json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
    assert outcome["exit"] == 0 and outcome["bytes"] > 0
    assert outcome["loaded"] == [], "a provider, embedding, database or settings module was loaded"


def test_there_is_no_live_mode_and_the_two_modes_are_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--live"], ["--i-understand-this-costs-money"], ["--api", "--in-process"]):
        with pytest.raises(SystemExit) as stopped:
            parse_args(argv)
        assert stopped.value.code == 2
    capsys.readouterr()
    assert parse_args([]).api is None  # in-process is the default


# --- 6. the API mode ---------------------------------------------------------------------------


def _wire(context: OkfContext) -> dict[str, Any]:
    """The context route's JSON, built through the public response models in `aida.schemas`.

    Not through the router's private helper: this proves the harness reads what the real
    response model carries, and an invented field or a wrong type fails validation here.
    """
    ids = citation_ids(context)
    read = OkfContextRead(
        context_product_version_id=uuid4(),
        product_key="fake",
        product_version=1,
        publication=OkfPublicationRead(
            publication_id=uuid4(),
            sequence=3,
            trigger="INITIAL",
            captured_at=datetime(2026, 9, 19, tzinfo=UTC),
            is_current=True,
            bundle_content_digest="b" * 64,
            content_snapshot_digest="c" * 64,
            document_count=21,
            rendered_count=21,
            carried_count=0,
            valid=True,
            changes=OkfChangeSummaryRead(
                added=[],
                changed=[],
                removed=[],
                changed_subjects=0,
                marked_subjects=0,
                full_render=True,
            ),
        ),
        status=context.status,
        question_terms=list(context.question_terms),
        documents=[
            OkfContextDocumentRead(
                citation=ids[document.path],
                path=document.path,
                sha256=document.sha256,
                type=document.type,
                title=document.title,
                status=document.status,
                description=document.description,
                hop=document.hop,
                score=document.score,
                matched_terms=list(document.matched_terms),
                linked_from=document.linked_from,
                approved_statements=list(document.approved),
                derived_statements=list(document.derived),
                sections=[
                    OkfContextSectionRead(
                        anchor=section.anchor,
                        heading=section.heading,
                        text=section.text,
                        rows_shown=section.rows_shown,
                        rows_total=section.rows_total,
                    )
                    for section in document.sections
                ],
            )
            for document in context.documents
        ],
        omitted=[
            OkfContextOmissionRead(
                path=item.path, anchor=item.anchor, reason=item.reason, chars=item.chars
            )
            for item in context.omitted
        ],
        omitted_count=context.omitted_count,
        ambiguous=list(context.ambiguous),
        max_chars=context.max_chars,
        used_chars=context.used_chars,
        guidance=context.guidance,
        markdown=render_markdown(context, product="fake"),
    )
    parsed: dict[str, Any] = json.loads(read.model_dump_json())
    return parsed


class FakeStack:
    """A running stack that answers with the real pipeline in the real response shape."""

    def __init__(
        self,
        bundle: FixtureBundle,
        *,
        source: bool,
        hide: tuple[str, ...] = (),
        refuse_context: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.source = source
        self.refuse_context = refuse_context
        self.bundle = bundle
        objects = [
            {
                "key": obj.key,
                "kind": obj.kind,
                "qualified_name": obj.qualified_name,
                "description_state": obj.description.state,
            }
            for obj in bundle.snapshot.objects
            if obj.name not in hide
        ]
        routines = [
            {
                "key": routine.key,
                "kind": "ROUTINE",
                "qualified_name": f"{routine.qualified_name}{routine.signature}",
                "description_state": routine.description.state,
            }
            for routine in bundle.snapshot.routines
        ]
        self.body = {
            "profile": "atlas-okf-export/4",
            "document_count": len(bundle.documents),
            "publication": {"publication_id": "p-1", "sequence": 3},
            "manifest": {"source_objects": [*objects, *routines]},
        }

    def __call__(self, method: str, url: str, headers: Any, body: Any) -> tuple[int, Any]:
        self.calls.append((method, url, dict(headers), body))
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        root = (
            f"/v1/datasources/{DATASOURCE}/okf-bundle"
            if self.source
            else f"/v1/context-product-versions/{VERSION}/okf-bundle"
        )
        if path.startswith("/v1/organizations?"):
            return 200, {"items": [{"id": ORG, "slug": "sample-bank", "name": "Sample Bank"}]}
        if path == f"/v1/organizations/{ORG}/datasources?limit=200":
            return 200, {
                "items": [{"id": DATASOURCE, "name": "Customer Master (Postgres, sample)"}]
            }
        if method == "GET" and path == root:
            return 200, self.body
        if method == "POST" and path == f"{root}/context":
            if self.refuse_context:
                return 403, {"detail": "NO_BINDING_FOR_DATASOURCE"}
            assert isinstance(body, dict)
            context = select_with_ranking(
                self.bundle.snapshot,
                self.bundle.documents,
                str(body["question"]),
                max_chars=int(body["max_chars"]),
            )
            return 200, _wire(context)
        return 404, {"detail": f"unexpected {method} {path}"}


def _source_bundle(corpus: Corpus) -> FixtureBundle:
    """A source bundle holds no concepts and no tools, so the fake serves the fixture without."""
    snapshot = replace(build_fixture_snapshot(corpus.estate), concepts=(), tools=())
    documents = {d.path: (d.text, d.sha256) for d in export_okf_bundle(snapshot).documents}
    inventory = frozenset(
        ref
        for path in document_subjects(snapshot)
        if (ref := _identify_path(documents, path)) is not None
    )
    return FixtureBundle(snapshot, documents, inventory, {})


def _identify_path(documents: dict[str, tuple[str, str]], path: str) -> ObjectRef | None:
    parsed = parse_document(path, *documents[path])
    return identify(parsed.type, parsed.title)


def _ask(stack: FakeStack, corpus: Corpus, *, product: bool = False) -> Report:
    target = discover_target(
        stack,
        "http://stack",
        org="sample-bank",
        datasource=None if product else "Customer Master",
        product_version=VERSION if product else None,
        principal="okf-context-benchmark",
        roles="Analyst",
    )
    return run_api(corpus, target, stack, "http://stack")


def test_the_api_response_shape_the_harness_reads_is_the_real_one() -> None:
    read = set(OkfContextRead.model_fields)
    assert {"status", "documents", "ambiguous", "max_chars", "used_chars", "omitted_count"} <= read
    assert {"path", "type", "title", "hop", "score", "sections"} <= set(
        OkfContextDocumentRead.model_fields
    )
    assert {"manifest", "publication", "profile", "document_count"} <= set(
        OkfBundleRead.model_fields
    )
    assert {"publication_id", "sequence"} <= set(OkfPublicationRead.model_fields)


def test_source_mode_reads_only_skips_what_it_cannot_ask_and_matches_in_process(
    corpus: Corpus,
) -> None:
    source = _source_bundle(corpus)
    stack = FakeStack(source, source=True)
    report = _ask(stack, corpus)
    assert report.mode == "api" and list(report.variants) == ["full"]
    # Reads only: GETs, and POSTs to the context route whose body is the question and a budget.
    for method, url, headers, body in stack.calls:
        assert method == "GET" or (method == "POST" and url.endswith("/okf-bundle/context")), url
        if method == "POST":
            assert set(body) == {"question", "max_chars"}
        assert headers["X-Roles"] == "Analyst" and headers["X-Principal-Type"] == "USER"
    assert all(call[2].get("X-Organization-Id") == ORG for call in stack.calls[1:])
    # A source bundle holds no concept or tool, so those cases are listed as skipped, not missed.
    skipped = {item["id"]: item["reason"] for item in report.skipped}
    assert {"concept-found-by-alias", "tool-account-summary", "alias-word-alone"} <= set(skipped)
    needs_product = {k for k, reason in skipped.items() if "needs a product bundle" in reason}
    fixture_text = {k for k, reason in skipped.items() if "fixture estate" in reason}
    assert len(needs_product) == 5 and needs_product | fixture_text == set(skipped)
    # Cases whose ground truth is text the overlay authored are skipped even where the object
    # exists: a stack without that text would score its own absence as a ranking miss.
    assert fixture_text == _FIXTURE_ONLY
    assert {case.id for case in corpus.cases if case.fixture_only} == _FIXTURE_ONLY
    measured = report.variants["full"][DEFAULT_MAX_CHARS]
    assert len(measured) == len(corpus.cases) - len(skipped) == 17
    # The same bundle, asked in-process, gives the same answers: the modes agree.
    for item in measured:
        local = score_case(
            item.case,
            selection_from_context(
                select_with_ranking(
                    source.snapshot,
                    source.documents,
                    item.case.question,
                    max_chars=DEFAULT_MAX_CHARS,
                )
            ),
        )
        assert (item.observed_status, item.rank, item.reached, item.delivered) == (
            local.observed_status,
            local.rank,
            local.reached,
            local.delivered,
        ), item.case.id
    assert report.estate["with_approved_description"] == 7 + 1  # seven tables and one routine


def test_product_mode_asks_the_concept_and_tool_cases_too_and_matches_in_process(
    corpus: Corpus, bundle: FixtureBundle, report: Report
) -> None:
    stack = FakeStack(bundle, source=False)
    asked = _ask(stack, corpus, product=True)
    assert {item["id"] for item in asked.skipped} == _FIXTURE_ONLY  # and nothing else
    got = {item.case.id: item for item in asked.variants["full"][DEFAULT_MAX_CHARS]}
    want = {item.case.id: item for item in report.variants["full"][DEFAULT_MAX_CHARS]}
    assert set(got) == set(want) - _FIXTURE_ONLY
    assert set(want) == {case.id for case in corpus.cases}
    for case_id, item in got.items():
        other = want[case_id]
        assert (item.observed_status, item.rank, item.reached) == (
            other.observed_status,
            other.rank,
            other.reached,
        ), case_id
    assert stack.calls[1][1].endswith(f"/v1/context-product-versions/{VERSION}/okf-bundle")


def test_a_question_about_an_object_the_estate_lacks_is_skipped_not_counted_a_miss(
    corpus: Corpus, bundle: FixtureBundle
) -> None:
    stack = FakeStack(bundle, source=False, hide=("fact_fraud_alerts",))
    asked = _ask(stack, corpus, product=True)
    absent = {
        item["id"]: item["reason"]
        for item in asked.skipped
        if "not in this estate" in item["reason"]
    }
    assert set(absent) == {
        "lexical-control-answers-without-enrichment",
        "gap-proposed-lineage-steers-nothing",
        "gap-proposed-lineage-supports-no-answer",
        "paraphrase-suspicious",
    }
    assert all("fact_fraud_alerts" in reason for reason in absent.values())
    posts = [call for call in stack.calls if call[0] == "POST"]
    assert (
        len(posts)
        == len(asked.variants["full"][DEFAULT_MAX_CHARS])
        == (len(corpus.cases) - len(_FIXTURE_ONLY) - len(absent))
    )


def test_a_refusal_is_reported_with_its_reason_and_a_hint_and_never_read_as_no_match(
    corpus: Corpus, bundle: FixtureBundle, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack(_source_bundle(corpus), source=True, refuse_context=True)
    with pytest.raises(ApiError, match=r"HTTP 403: NO_BINDING_FOR_DATASOURCE.*--roles"):
        _ask(stack, corpus)
    assert (
        main(["--api", "http://stack", "--org", ORG, "--datasource", DATASOURCE], transport=stack)
        == 2
    )
    assert "NO_BINDING_FOR_DATASOURCE" in capsys.readouterr().err


def test_headers_are_the_development_identity_the_seed_script_sends() -> None:
    headers = api_headers("someone", "Analyst", ORG)
    assert headers["X-Principal-Id"] == "someone"
    assert headers["X-Principal-Type"] == "USER"
    assert headers["X-Organization-Id"] == ORG
    assert "X-Organization-Id" not in api_headers("someone", "Analyst", None)


# --- the command line --------------------------------------------------------------------------


def test_command_line_refuses_an_out_of_range_budget_and_an_unknown_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--max-chars", "999"]) == 2
    assert "--max-chars 999" in capsys.readouterr().err
    assert main(["--case", "no-such-case"]) == 2
    assert "no such case" in capsys.readouterr().err
    assert main(["--check-corpus"]) == 0
    assert "28 cases resolve" in capsys.readouterr().out


def test_the_markdown_report_states_it_is_retrieval_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--format", "markdown", "--max-chars", "16000", "--max-chars", "2000"]) == 0
    text = capsys.readouterr().out
    assert "RETRIEVAL ONLY -- not answer quality" in text
    assert "No acceptance threshold is set" in text
    assert "| variant | hit@1 |" in text and "Budget sweep" in text
