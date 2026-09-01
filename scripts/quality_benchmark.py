#!/usr/bin/env python3
"""Retrieval- and generation-path quality/accuracy benchmarks (tracker AG-8).

This is the *quality* counterpart to `scripts/perf_baseline.py` (PF-3): where PF-3
times a handful of hot paths, this script measures whether they return the right
answer. It is deliberately not, and does not attempt to be, the bank-scale
1M-object retrieval benchmark tracked separately as RT-8/PF-1 -- those need real
infrastructure (a populated warehouse-scale catalog, a soak rig) this sandbox does
not have. What this script *does* provide, following the exact ratchet pattern
PF-3 established:

    1. Seed a small, deterministic, synthetic-but-structured catalog
       (`seed_catalog`) -- fixed `uuid5` ids, not `uuid4()`-random, so results are
       byte-for-byte reproducible across runs and machines. No production data is
       read or required (ADR-0014 applies here too: this is fixture data authored
       for this benchmark, the same convention `tests/fixtures/adversarial_sql_corpus`
       (QG-1) and `test_rt7_quality_trust_ranking.py`'s seeded scenario already use).
    2. Run two checked-in corpora
       (`tests/fixtures/quality_benchmark_corpus/*.json`) through the REAL live
       code paths -- `aida.agent_intelligence.GovernedRetriever.retrieve`
       (-> `aida.retrieval.hybrid_retrieve_enhanced`, the same hybrid lexical +
       vector + graph + fusion pipeline wired into
       `agent_orchestrator.GovernedAgentOrchestrator`) and
       `aida.agent_intelligence.GovernedPlanner.plan` (the same tool-first/
       generation-fallback decision the live orchestrator's PLANNED state makes) --
       and score the result against each corpus's expectations.
    3. Compare the measured metrics against a committed baseline
       (`Docs/90-reference/quality-benchmark-baseline.json`) and fail if any
       tracked metric drops by more than `--threshold-points` percentage points.
       `--accept-baseline` regenerates it deliberately, exactly like PF-3.
    4. Publish a timestamped, reproducible results report
       (`Docs/90-reference/quality-benchmark-results.md`) on every run (unless
       `--no-report`).

What is measured with real numbers, and what stays honestly framework-only:

  - **Retrieval quality (real numbers)**: hit@1, a recall metric bounded by each
    corpus case's own `min_rank`, and MRR, from `retrieval_quality_corpus.json`
    run through the real `GovernedRetriever.retrieve`. The vector-similarity
    signal is real code, genuinely invoked -- but in any environment with no
    embedding-provider credentials configured (this sandbox included) it is
    honestly skipped (see `VectorSignalPosture` below and
    `retrieval.py`'s own "vector stage runs only with a real embedding model
    behind it" comment); the numbers below are the real fused result of
    lexical + graph + fusion with that one signal absent, not a partial run
    disguised as a complete one.
  - **Tool/generation-path *selection* quality (real numbers)**: pass rate from
    `tool_selection_corpus.json` run through the real `GovernedPlanner.plan` --
    whether the planner picks the approved governed tool, falls back to
    (simulated) controlled SQL, or requires model generation. This needs no live
    model route: tool-first selection is entirely a PLANNED-state decision,
    upstream of GENERATED (module 13 Sec. 3).
  - **Model *generation* quality (framework only in this sandbox)**: actually
    scoring generated SQL/answer text requires an approved, selected, credentialed,
    adapter-registered, explicitly-enabled model route (module 15 Sec. 6, all five
    conditions). `ModelGenerationPosture` below checks those conditions at the
    `Settings` level; this sandbox has `model_generation_enabled=False` and no
    `OPENAI_API_KEY`/`GEMINI_API_KEY`, so generation is not activatable here and no
    generation numbers are fabricated. When a real, approved route is configured,
    this posture check will report ACTIVATABLE and this file's `--help` output
    documents the follow-up (running scenarios through
    `model_gateway.ProviderNeutralModelGateway.structured_completion`) -- this
    script deliberately does not attempt a live network call on its own, to avoid
    an unbounded-cost, unbounded-network side effect from a routine benchmark run.

Usage:
    # CI / local gate: compare current metrics to the committed baseline; exit 1
    # on an unacknowledged regression. Also (re)writes the results report.
    uv run python scripts/quality_benchmark.py

    # After a deliberate, reviewed change to retrieval or planning behaviour:
    # regenerate the baseline from current measurements and commit it.
    uv run python scripts/quality_benchmark.py --accept-baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "quality_benchmark_corpus"
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "quality-benchmark-baseline.json"
DEFAULT_REPORT = REPO_ROOT / "Docs" / "90-reference" / "quality-benchmark-results.md"

# Percentage points (0-100 scale), matching PF-3's percent-based threshold shape.
DEFAULT_THRESHOLD_POINTS = 5.0


def _fixed_id(*parts: str) -> UUID:
    """Deterministic id from a name, not `uuid4()`-random -- same technique
    `perf_baseline.py`'s policy-engine benchmark already uses, so this benchmark's
    catalog is byte-for-byte reproducible across machines and runs."""
    return uuid5(NAMESPACE_URL, "quality-benchmark:" + ":".join(parts))


# ---------------------------------------------------------------------------
# Seeded catalog (synthetic, structured, deterministic -- no production data)
# ---------------------------------------------------------------------------

TABLE_SEEDS: tuple[tuple[str, str], ...] = (
    ("fact_orders", "Order transactions placed by retail customers."),
    ("dim_customer", "Customer dimension with account holder details."),
    ("dim_product", "Product catalog dimension."),
    ("fact_payments", "Payment transactions processed across channels."),
    ("dim_branch", "Bank branch location dimension."),
    ("fact_loan_applications", "Loan application submissions and status."),
    ("dim_employee", "Employee roster dimension."),
    ("fact_account_balances", "Daily account balance snapshots."),
    ("dim_merchant", "Merchant dimension for card transactions."),
    ("fact_fraud_alerts", "Fraud alert events raised by the monitoring system."),
)

TOOL_SEEDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "customer-account-summary",
        "Customer Account Summary",
        "Approved lookup for a customer's account summary.",
        "public.dim_customer",
    ),
)


@dataclass(frozen=True, slots=True)
class SeededCatalog:
    """Ids of the objects `seed_catalog` created, keyed by the stable slug the
    corpus JSON files reference -- never a raw UUID a fixture author would have
    to keep in sync by hand."""

    datasource_id: UUID
    table_ids: dict[str, UUID]
    tool_version_ids: dict[str, UUID]


async def seed_catalog(session: AsyncSession) -> SeededCatalog:
    """Build one small, deterministic retail-bank catalog: ten tables across
    distinct subject areas (so a lexical/fusion match is unambiguous), one FK
    (fact_orders -> dim_customer, so graph expansion has a real edge to walk),
    and one governed tool bound to dim_customer -- the same seeded-scenario shape
    `tests/test_rt7_quality_trust_ranking.py` and
    `tests/test_agent_orchestrator_retrieval_wiring.py` already use, just broader.
    """
    from aida.models import (
        DataDomain,
        DataSource,
        GovernedTool,
        GovernedToolVersion,
        LineOfBusiness,
        MetadataCatalog,
        MetadataConstraint,
        MetadataSchema,
        MetadataTable,
        Organization,
        Project,
    )

    org = Organization(
        id=_fixed_id("org"), name="Bank", slug="quality-benchmark-bank"
    )
    session.add(org)
    await session.flush()

    lob = LineOfBusiness(
        id=_fixed_id("lob"), organization_id=org.id, name="Retail", code="RETAIL"
    )
    session.add(lob)
    await session.flush()

    domain = DataDomain(
        id=_fixed_id("domain"),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Commerce",
        code="COMMERCE",
    )
    session.add(domain)
    await session.flush()

    project = Project(
        id=_fixed_id("project"),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Commerce",
        slug="core-commerce",
    )
    session.add(project)
    await session.flush()

    datasource = DataSource(
        id=_fixed_id("datasource"),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="core-warehouse",
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PRODUCTION",
        credential_reference="vault://core-warehouse",
    )
    session.add(datasource)
    await session.flush()

    catalog = MetadataCatalog(
        id=_fixed_id("catalog"),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="warehouse",
        fingerprint="fp-quality-benchmark-catalog",
    )
    session.add(catalog)
    await session.flush()

    schema = MetadataSchema(
        id=_fixed_id("schema"),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp-quality-benchmark-schema",
    )
    session.add(schema)
    await session.flush()

    table_ids: dict[str, UUID] = {}
    for name, description in TABLE_SEEDS:
        table_id = _fixed_id("table", name)
        table_ids[name] = table_id
        session.add(
            MetadataTable(
                id=table_id,
                organization_id=org.id,
                datasource_id=datasource.id,
                schema_id=schema.id,
                name=name,
                object_type="TABLE",
                status="ACTIVE",
                fingerprint=f"fp-quality-benchmark-{name}",
                source_description=description,
            )
        )
    await session.flush()

    session.add(
        MetadataConstraint(
            id=_fixed_id("constraint", "fk_orders_customer"),
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=table_ids["fact_orders"],
            name="fk_orders_customer",
            constraint_type="FOREIGN_KEY",
            columns=["customer_id"],
            referenced_table_id=table_ids["dim_customer"],
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp-quality-benchmark-fk-orders-customer",
        )
    )

    tool_version_ids: dict[str, UUID] = {}
    for slug, name, description, referenced_table in TOOL_SEEDS:
        tool_id = _fixed_id("tool", slug)
        version_id = _fixed_id("tool-version", slug, "1")
        tool_version_ids[slug] = version_id
        session.add(
            GovernedTool(id=tool_id, organization_id=org.id, project_id=project.id, slug=slug)
        )
        await session.flush()
        session.add(
            GovernedToolVersion(
                id=version_id,
                organization_id=org.id,
                tool_id=tool_id,
                version=1,
                status="PUBLISHED",
                name=name,
                description=description,
                datasource_id=datasource.id,
                # Fixture text only, from the fixed TOOL_SEEDS tuple above, never executed by
                # anything: sql_template is inert metadata this benchmark reads back.
                sql_template=f"SELECT * FROM {referenced_table} WHERE 1=1",  # noqa: S608
                referenced_tables=[referenced_table],
                parameter_schema=[],
                allowed_roles=["Analyst"],
                fingerprint=f"fp-quality-benchmark-tool-{slug}",
                created_by="quality-benchmark",
            )
        )
    await session.flush()

    return SeededCatalog(
        datasource_id=datasource.id, table_ids=table_ids, tool_version_ids=tool_version_ids
    )


async def _make_session() -> tuple[AsyncSession, object]:
    """A fresh in-memory sqlite database with every table registered -- the same
    pattern `test_rt7_quality_trust_ranking.py`'s `db` fixture uses. Returns the
    session and the engine (caller disposes the engine when done)."""
    import aida.models  # noqa: F401 -- registers every table on Base.metadata
    from aida.db import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return maker(), engine


# ---------------------------------------------------------------------------
# Retrieval quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    id: str
    question: str
    expected_object_type: str
    expected_object_key: str
    min_rank: int


@dataclass(frozen=True, slots=True)
class RetrievalCaseResult:
    case: RetrievalCase
    rank: int | None
    reciprocal_rank: float

    @property
    def hit_at_1(self) -> bool:
        return self.rank == 1

    @property
    def within_bound(self) -> bool:
        return self.rank is not None and self.rank <= self.case.min_rank


@dataclass(frozen=True, slots=True)
class RetrievalQualityReport:
    results: list[RetrievalCaseResult]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def hit_at_1_rate(self) -> float:
        return _rate(r.hit_at_1 for r in self.results)

    @property
    def recall_within_bound_rate(self) -> float:
        return _rate(r.within_bound for r in self.results)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.reciprocal_rank for r in self.results) / len(self.results)


def _rate(flags: Iterable[bool]) -> float:
    values = list(flags)
    if not values:
        return 0.0
    return sum(1 for v in values if v) / len(values)


def load_retrieval_corpus(path: Path) -> list[RetrievalCase]:
    data = json.loads(path.read_text())
    return [
        RetrievalCase(
            id=case["id"],
            question=case["question"],
            expected_object_type=case["expected_object_type"],
            expected_object_key=case["expected_object_key"],
            min_rank=case["min_rank"],
        )
        for case in data["cases"]
    ]


async def run_retrieval_benchmark(
    session: AsyncSession, catalog: SeededCatalog, cases: list[RetrievalCase]
) -> RetrievalQualityReport:
    from aida.agent_intelligence import GovernedRetriever
    from aida.config import Settings
    from aida.models import DataSource

    datasource = await session.get(DataSource, catalog.datasource_id)
    if datasource is None:
        raise RuntimeError("seeded datasource missing -- seed_catalog did not commit")

    def _expected_object_id(case: RetrievalCase) -> str:
        if case.expected_object_type == "TABLE":
            return str(catalog.table_ids[case.expected_object_key])
        if case.expected_object_type == "GOVERNED_TOOL":
            return str(catalog.tool_version_ids[case.expected_object_key])
        raise ValueError(f"unsupported expected_object_type: {case.expected_object_type!r}")

    retriever = GovernedRetriever(Settings())
    results: list[RetrievalCaseResult] = []
    for case in cases:
        hits = await retriever.retrieve(session, datasource=datasource, question=case.question)
        expected_id = _expected_object_id(case)
        rank = next(
            (
                idx + 1
                for idx, hit in enumerate(hits)
                if hit.object_type == case.expected_object_type and hit.object_id == expected_id
            ),
            None,
        )
        reciprocal_rank = 1.0 / rank if rank else 0.0
        results.append(RetrievalCaseResult(case=case, rank=rank, reciprocal_rank=reciprocal_rank))
    return RetrievalQualityReport(results=results)


# ---------------------------------------------------------------------------
# Tool / generation-path selection quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSelectionCase:
    id: str
    question: str
    roles: frozenset[str]
    candidate_sql_available: bool
    expected_strategy: str
    expected_tool_key: str | None


@dataclass(frozen=True, slots=True)
class ToolSelectionCaseResult:
    case: ToolSelectionCase
    actual_strategy: str
    actual_tool_key: str | None

    @property
    def passed(self) -> bool:
        return (
            self.actual_strategy == self.case.expected_strategy
            and self.actual_tool_key == self.case.expected_tool_key
        )


@dataclass(frozen=True, slots=True)
class ToolSelectionReport:
    results: list[ToolSelectionCaseResult]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return _rate(r.passed for r in self.results)


def load_tool_selection_corpus(path: Path) -> list[ToolSelectionCase]:
    data = json.loads(path.read_text())
    return [
        ToolSelectionCase(
            id=case["id"],
            question=case["question"],
            roles=frozenset(case["roles"]),
            candidate_sql_available=case["candidate_sql_available"],
            expected_strategy=case["expected_strategy"],
            expected_tool_key=case["expected_tool_key"],
        )
        for case in data["cases"]
    ]


async def run_tool_selection_benchmark(
    session: AsyncSession, catalog: SeededCatalog, cases: list[ToolSelectionCase]
) -> ToolSelectionReport:
    from aida.agent_intelligence import GovernedPlanner, GovernedRetriever
    from aida.config import Settings
    from aida.models import DataSource

    datasource = await session.get(DataSource, catalog.datasource_id)
    if datasource is None:
        raise RuntimeError("seeded datasource missing -- seed_catalog did not commit")

    settings = Settings()
    retriever = GovernedRetriever(settings)
    planner = GovernedPlanner(settings)
    tool_key_by_version_id = {str(v): k for k, v in catalog.tool_version_ids.items()}

    results: list[ToolSelectionCaseResult] = []
    for case in cases:
        hits = await retriever.retrieve(session, datasource=datasource, question=case.question)
        plan = planner.plan(
            retrieval_hits=hits,
            roles=case.roles,
            candidate_sql_available=case.candidate_sql_available,
            tool_parameters={},
        )
        actual_tool_key = (
            tool_key_by_version_id.get(plan.selected_tool_version_id)
            if plan.selected_tool_version_id
            else None
        )
        results.append(
            ToolSelectionCaseResult(
                case=case, actual_strategy=plan.strategy, actual_tool_key=actual_tool_key
            )
        )
    return ToolSelectionReport(results=results)


# ---------------------------------------------------------------------------
# Model-generation activation posture (framework only unless a route is live)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelGenerationPosture:
    model_generation_enabled: bool
    model_route_configured: bool
    openai_credential_present: bool
    gemini_credential_present: bool
    vector_signal_available: bool
    vector_signal_reason: str | None

    @property
    def activatable(self) -> bool:
        """Settings-level check only -- NOT the full five-condition activation
        posture module 15 defines (that also needs an approved+selected
        `ApprovedModelRoute` row and a resolving credential reference, which this
        benchmark does not provision). This is a necessary-but-not-sufficient
        signal: false here means generation numbers are definitely unavailable;
        true means the *sandbox* has what a route would need, not that one exists.
        """
        return self.model_generation_enabled and self.model_route_configured and (
            self.openai_credential_present or self.gemini_credential_present
        )


def check_model_generation_posture() -> ModelGenerationPosture:
    from aida.config import Settings
    from aida.embedding_provider import EmbeddingUnavailable, resolve_embedding_provider
    from aida.secrets import SecretResolver

    settings = Settings()

    def _has_secret(secret: object) -> bool:
        if secret is None:
            return False
        value = secret.get_secret_value()  # type: ignore[attr-defined]
        return bool(value) and not value.startswith("replace-")

    vector_available = True
    vector_reason: str | None = None
    try:
        resolve_embedding_provider(settings, SecretResolver(settings))
    except EmbeddingUnavailable as exc:
        vector_available = False
        vector_reason = str(exc)

    return ModelGenerationPosture(
        model_generation_enabled=settings.model_generation_enabled,
        model_route_configured=settings.model_route is not None,
        openai_credential_present=_has_secret(settings.openai_api_key),
        gemini_credential_present=_has_secret(settings.gemini_api_key),
        vector_signal_available=vector_available,
        vector_signal_reason=vector_reason,
    )


# ---------------------------------------------------------------------------
# Baseline comparison (same ratchet pattern as perf_baseline.py)
# ---------------------------------------------------------------------------

TRACKED_METRICS = (
    "retrieval_hit_at_1_rate",
    "retrieval_recall_within_bound_rate",
    "retrieval_mrr",
    "tool_selection_pass_rate",
)


@dataclass(frozen=True, slots=True)
class MetricRegression:
    name: str
    baseline: float
    current: float
    point_change: float

    def __str__(self) -> str:
        return (
            f"[REGRESSION] {self.name}: {self.baseline:.4f} -> {self.current:.4f} "
            f"({self.point_change:+.2f} points)"
        )


def find_regressions(
    baseline: dict[str, float], current: dict[str, float], *, threshold_points: float
) -> list[MetricRegression]:
    regressions: list[MetricRegression] = []
    for name, base in baseline.items():
        if name not in current:
            continue
        point_change = (current[name] - base) * 100
        if point_change < -threshold_points:
            regressions.append(
                MetricRegression(
                    name=name, baseline=base, current=current[name], point_change=point_change
                )
            )
    return regressions


def _load_baseline(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"baseline at {path} has no 'metrics' object")
    return {str(name): float(entry["value"]) for name, entry in metrics.items()}


def _write_baseline(
    path: Path, current: dict[str, float], *, threshold_points: float, case_counts: dict[str, int]
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "threshold_points": threshold_points,
        "metrics": {
            name: {"value": round(value, 4), "case_count": case_counts.get(name, 0)}
            for name, value in current.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Results report
# ---------------------------------------------------------------------------


def _write_report(
    path: Path,
    *,
    retrieval: RetrievalQualityReport,
    tool_selection: ToolSelectionReport,
    posture: ModelGenerationPosture,
    current_metrics: dict[str, float],
    baseline_metrics: dict[str, float] | None,
    regressions: list[MetricRegression],
) -> None:
    lines: list[str] = []
    lines.append("# Quality benchmark results (AG-8)")
    lines.append("")
    lines.append(
        f"Generated {datetime.now(UTC).isoformat()} by `scripts/quality_benchmark.py`. "
        "Reproduce with `uv run python scripts/quality_benchmark.py` (requires "
        "`AIDA_ENVIRONMENT` set, e.g. `development`). Every number below comes from a "
        "real run of the live retrieval/planning code against the deterministic seeded "
        "catalog in that script's `seed_catalog` -- not hand-typed."
    )
    lines.append("")
    lines.append(
        "Scope: this is the quality/accuracy counterpart to PF-3's latency ratchet "
        "(`Docs/90-reference/perf-baseline.json`), not the bank-scale 1M-object benchmark "
        "tracked separately as RT-8/PF-1, which this sandbox has no infrastructure for."
    )
    lines.append("")

    lines.append("## Retrieval quality")
    lines.append("")
    lines.append(
        f"`GovernedRetriever.retrieve` (-> `hybrid_retrieve_enhanced`) over "
        f"`tests/fixtures/quality_benchmark_corpus/retrieval_quality_corpus.json` "
        f"({retrieval.case_count} cases)."
    )
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    for name in ("retrieval_hit_at_1_rate", "retrieval_recall_within_bound_rate", "retrieval_mrr"):
        lines.append(_metric_row(name, current_metrics, baseline_metrics))
    lines.append("")
    lines.append("| Case | Question | Expected | Rank | Hit@1 | Within bound |")
    lines.append("|---|---|---|---|---|---|")
    for r in retrieval.results:
        expected = f"{r.case.expected_object_type}:{r.case.expected_object_key}"
        rank_display = str(r.rank) if r.rank is not None else "not found"
        lines.append(
            f"| {r.case.id} | {r.case.question} | {expected} | {rank_display} | "
            f"{'yes' if r.hit_at_1 else 'no'} | {'yes' if r.within_bound else 'no'} |"
        )
    lines.append("")
    lines.append(
        "Vector-similarity signal: "
        + (
            "available and exercised."
            if posture.vector_signal_available
            else f"skipped this run — `{posture.vector_signal_reason}`. "
            "The numbers above are the real fused result of lexical + graph + fusion "
            "with the vector signal absent, not a partial run presented as complete."
        )
    )
    lines.append("")

    lines.append("## Tool / generation-path selection quality")
    lines.append("")
    lines.append(
        f"`GovernedPlanner.plan` over "
        f"`tests/fixtures/quality_benchmark_corpus/tool_selection_corpus.json` "
        f"({tool_selection.case_count} cases) -- no live model route needed, since "
        "tool-first selection is a PLANNED-state decision upstream of GENERATED."
    )
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(_metric_row("tool_selection_pass_rate", current_metrics, baseline_metrics))
    lines.append("")
    lines.append("| Case | Question | Roles | Expected strategy | Actual strategy | Passed |")
    lines.append("|---|---|---|---|---|---|")
    for r in tool_selection.results:
        roles = ",".join(sorted(r.case.roles))
        lines.append(
            f"| {r.case.id} | {r.case.question} | {roles} | {r.case.expected_strategy} | "
            f"{r.actual_strategy} | {'yes' if r.passed else 'no'} |"
        )
    lines.append("")

    lines.append("## Model generation quality (framework only in this sandbox)")
    lines.append("")
    lines.append("| Activation prerequisite | Status |")
    lines.append("|---|---|")
    lines.append(f"| `model_generation_enabled` | {posture.model_generation_enabled} |")
    lines.append(f"| `model_route` configured | {posture.model_route_configured} |")
    lines.append(f"| OpenAI credential present | {posture.openai_credential_present} |")
    lines.append(f"| Gemini credential present | {posture.gemini_credential_present} |")
    lines.append(f"| **Activatable in this environment** | **{posture.activatable}** |")
    lines.append("")
    if posture.activatable:
        lines.append(
            "This environment has the `Settings`-level prerequisites for a live model "
            "route (module 15's five-condition posture also needs an approved+selected "
            "`ApprovedModelRoute` row, which this script does not provision). This script "
            "does not itself place a live network call to a model provider — running actual "
            "generated-SQL/answer scenarios through "
            "`model_gateway.ProviderNeutralModelGateway.structured_completion` against a "
            "real approved route is the deliberate next step once one is provisioned in "
            "this environment, kept out of a routine benchmark run to avoid an "
            "unbounded-cost, unbounded-network side effect."
        )
    else:
        lines.append(
            "No usable model route in this sandbox: `model_generation_enabled` is False "
            "and neither `OPENAI_API_KEY` nor `GEMINI_API_KEY` is configured. This section "
            "is honestly framework-only — the harness above (posture check + the real, "
            "model-free tool/generation-path selection benchmark) is real and running; "
            "actual generated-text quality numbers require a configured, approved model "
            "route and are not fabricated here."
        )
    lines.append("")

    if regressions:
        lines.append("## Regressions against the committed baseline")
        lines.append("")
        for r in regressions:
            lines.append(f"- {r}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _metric_row(
    name: str, current_metrics: dict[str, float], baseline_metrics: dict[str, float] | None
) -> str:
    current = current_metrics.get(name)
    current_display = f"{current:.4f}" if current is not None else "n/a"
    base = baseline_metrics.get(name) if baseline_metrics else None
    if base is None:
        return f"| `{name}` | {current_display} | n/a (new) | — |"
    change = (current - base) * 100 if current is not None else 0.0
    return f"| `{name}` | {current_display} | {base:.4f} | {change:+.2f} pts |"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _run(
    *, retrieval_corpus_path: Path, tool_selection_corpus_path: Path
) -> tuple[RetrievalQualityReport, ToolSelectionReport, ModelGenerationPosture]:
    session, engine = await _make_session()
    try:
        catalog = await seed_catalog(session)
        retrieval_cases = load_retrieval_corpus(retrieval_corpus_path)
        tool_cases = load_tool_selection_corpus(tool_selection_corpus_path)
        retrieval_report = await run_retrieval_benchmark(session, catalog, retrieval_cases)
        tool_report = await run_tool_selection_benchmark(session, catalog, tool_cases)
        posture = check_model_generation_posture()
    finally:
        await session.close()
        await engine.dispose()
    return retrieval_report, tool_report, posture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-report", action="store_true", help="Do not write the results report.")
    parser.add_argument(
        "--retrieval-corpus",
        type=Path,
        default=CORPUS_DIR / "retrieval_quality_corpus.json",
    )
    parser.add_argument(
        "--tool-selection-corpus",
        type=Path,
        default=CORPUS_DIR / "tool_selection_corpus.json",
    )
    parser.add_argument(
        "--accept-baseline",
        action="store_true",
        help=(
            "Regenerate the baseline from current measurements and write it. Run this "
            "deliberately after reviewing the reported change, then commit the updated "
            "baseline file -- the same explicit, auditable path `perf_baseline.py` provides."
        ),
    )
    parser.add_argument("--threshold-points", type=float, default=DEFAULT_THRESHOLD_POINTS)
    args = parser.parse_args(argv)

    if not args.accept_baseline and not args.baseline.exists():
        print(f"::error::No quality baseline found at {args.baseline}.")
        print("Run `uv run python scripts/quality_benchmark.py --accept-baseline` to create one.")
        return 1

    retrieval_report, tool_report, posture = asyncio.run(
        _run(
            retrieval_corpus_path=args.retrieval_corpus,
            tool_selection_corpus_path=args.tool_selection_corpus,
        )
    )

    current_metrics = {
        "retrieval_hit_at_1_rate": retrieval_report.hit_at_1_rate,
        "retrieval_recall_within_bound_rate": retrieval_report.recall_within_bound_rate,
        "retrieval_mrr": retrieval_report.mrr,
        "tool_selection_pass_rate": tool_report.pass_rate,
    }
    case_counts = {
        "retrieval_hit_at_1_rate": retrieval_report.case_count,
        "retrieval_recall_within_bound_rate": retrieval_report.case_count,
        "retrieval_mrr": retrieval_report.case_count,
        "tool_selection_pass_rate": tool_report.case_count,
    }

    print("Retrieval quality:")
    print(f"  hit@1:                 {retrieval_report.hit_at_1_rate:.4f}")
    print(f"  recall (within bound): {retrieval_report.recall_within_bound_rate:.4f}")
    print(f"  MRR:                   {retrieval_report.mrr:.4f}")
    print("Tool/generation-path selection quality:")
    print(f"  pass rate:             {tool_report.pass_rate:.4f}")
    print("Model generation posture:")
    print(f"  activatable in this environment: {posture.activatable}")
    if not posture.vector_signal_available:
        print(f"  vector signal skipped: {posture.vector_signal_reason}")

    if args.accept_baseline:
        _write_baseline(
            args.baseline,
            current_metrics,
            threshold_points=args.threshold_points,
            case_counts=case_counts,
        )
        print(f"\nBaseline regenerated at {args.baseline}.")
        if not args.no_report:
            _write_report(
                args.report,
                retrieval=retrieval_report,
                tool_selection=tool_report,
                posture=posture,
                current_metrics=current_metrics,
                baseline_metrics=current_metrics,
                regressions=[],
            )
            print(f"Report written to {args.report}.")
        return 0

    baseline_metrics = _load_baseline(args.baseline)
    regressions = find_regressions(
        baseline_metrics, current_metrics, threshold_points=args.threshold_points
    )

    if not args.no_report:
        _write_report(
            args.report,
            retrieval=retrieval_report,
            tool_selection=tool_report,
            posture=posture,
            current_metrics=current_metrics,
            baseline_metrics=baseline_metrics,
            regressions=regressions,
        )
        print(f"\nReport written to {args.report}.")

    if not regressions:
        print(
            f"\nNo quality regressions beyond {args.threshold_points} points "
            f"against {args.baseline}."
        )
        return 0

    print(f"\n{len(regressions)} quality regression(s) against {args.baseline}:")
    for r in regressions:
        print(r)
    print(
        "\n::error::Quality regression(s) detected. If this is an expected consequence of a "
        "deliberate change to retrieval or planning behaviour: review why the metric moved, "
        "then run `uv run python scripts/quality_benchmark.py --accept-baseline` and commit "
        "the refreshed baseline. If it wasn't expected, fix the regression instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
