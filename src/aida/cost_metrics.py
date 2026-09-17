"""R11-FP17: what a parse and a model call cost, per source, without lying about it.

The review asks for "parser/model cost metrics" per source. Before this module
there were none: no `Counter`, `Histogram` or `Gauge` anywhere in
`cost_showback.py`, `model_gateway.py`, `agent_budget.py` or `mcp_budget.py`.
What existed was token accounting in database rows, per agent contract
(`aida.agent_budget`), and a per-LOB consumption roll-up over `QueryExecution`
rows (`aida.cost_showback`). Neither is per source, and neither is a metric.

Two decisions shape everything below, and both are the kind that are cheap to
get wrong quietly.

---------------------------------------------------------------------------
**1. Showback, not chargeback -- and the units stay apart.**

`aida.cost_showback`'s module docstring is the standing decision this module
does not get to relax: the `cost_record` ledger does not exist, nothing in this
platform meters a dollar, and `query_gateway.gate_query_estimate`'s `plan_cost`
is a connector-shaped proxy -- bytes scanned for a byte-billed engine like
BigQuery, a heuristic planner score for everything else -- which is *not
comparable to itself* across connectors, let alone to a token.

So:

* **No series here is denominated in money.** There is no `aida_*_cost_dollars`
  and there will not be one until something bills.
* **Provider-reported and estimated tokens are the same series with different
  `basis` labels, never one number.** `ModelCallEvidence` already keeps
  `provider_input_tokens` (what OpenAI or Gemini said it billed, or `None`) and
  `estimated_input_tokens` (this platform's 4-bytes-per-token heuristic, which
  is what the input cap is enforced against) apart. Summing them would produce
  a figure that is neither. `basis="REPORTED"` is billable; `basis="ESTIMATED"`
  is not, and `atlas:model_tokens_reported_share:rate1h` in
  `infra/monitoring/` makes the mix visible instead of leaving it to be
  assumed.
* **`plan_cost` is not added to anything.** It is not carried here at all.
  `cost_showback` reports it, per LOB, with `COST_BASIS` attached saying it is
  not comparable across connectors; that is the right place for it and the
  right caveat. A quota or a metric that added planner score to token count
  would be a number with no unit.
* **Parser cost is time and volume, not price.** A parse consumes CPU on this
  platform's own workers, so the honest units are seconds, statements and
  bytes of (already value-free, already redacted) SQL. Turning that into money
  needs a price per CPU-second that nobody here has.

---------------------------------------------------------------------------
**2. Per-source attribution lives in rows, not in a label.**

The review wants the source dimension, and a `datasource_id` label would be the
straightforward way to get it. It is also unbounded metric cardinality --
exactly F17 in the 2026-09-05 review, which `footprint_metrics.py`,
`projection_metrics.py` and `retrieval_metrics.py` all already refuse on those
grounds. The hazard is not theoretical for these series in particular: a model
call happens per drafting batch and a parse happens per view definition, so
these are the *highest*-frequency series in the platform after the HTTP
middleware. A thousand-source estate multiplied by the label sets below is tens
of thousands of series, per process, forever -- and a datasource id on a
scrapeable surface is also a tenant identifier on a public one.

**The deliberate choice: two surfaces, each carrying what it can bound.**

* The **metric surface** is labelled by closed sets only, so its cardinality is
  a product of small constants that can be counted from this file: model
  series by `provider_type` (the registered adapters), `direction` (input or
  output) and `basis` (reported or estimated); parser series by `parser` (the
  entry points named in `Parser`) and `dialect` (the five sqlglot dialects the
  parsers support, plus `other`). `dialect` is the source-shaped dimension that
  *is* bounded, and it is genuinely useful -- a T-SQL estate and a BigQuery
  estate have different parse costs, and that is visible here.
* The **per-source figures** go to `aida.usage_quotas`' `source_usage_window`
  rows, one per source per dimension per UTC day. Per-source attribution the
  database bounds, joinable to `DataSource`, `line_of_business_id` and
  therefore to `cost_showback`'s existing LOB roll-up, and subject to the same
  tenant isolation as every other governed row. That is strictly *more* useful
  per source than a Prometheus label, because it survives scrape retention and
  can be grouped by tenancy columns a metric does not have.

What is genuinely lost: no per-source alerting rule can be written from
Prometheus alone. That is the same consequence `footprint_metrics.py` already
accepted and documented, and `infra/monitoring/prometheus/rules/atlas.rules.yml`
says so in its header rather than pretending otherwise.

---------------------------------------------------------------------------
**Where the numbers come from.**

`observe_model_call` is called by `ProviderNeutralModelGateway.
structured_completion` itself, so every caller of the gateway is counted --
including `aida.agent_orchestrator`'s, which this change does not touch. It
takes no session and writes no row, so it adds no database work to the model
path.

`record_model_spend` is the per-source half and needs a session and a
datasource, which the gateway does not have (it is given an `organization_id`
and a route, not a source). It is therefore called by the callers that do know
their source. **Not every model call path is attributed to a source yet**, and
the ones that are not are visible rather than silently absent:
`aida_model_spend_attribution_total{attributed="false"}` counts them.

`parser_span` measures a parse and is called inside `parse_view_lineage`, so
every view-definition parse in the platform is timed wherever it is invoked
from. `record_parser_spend` is the per-source half, on the same footing as the
model one.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

import structlog
from prometheus_client import Counter, Histogram

if TYPE_CHECKING:  # pragma: no cover -- typing only
    # Behind TYPE_CHECKING for the same reason as the deferred import below:
    # `aida.sql_lineage_parser` imports this module, and keeping SQLAlchemy out
    # of that module's *runtime* graph is part of the database-free guarantee
    # its docstring makes. `from __future__ import annotations` makes every
    # annotation a string, so the type is still checked and never imported.
    from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

# `aida.usage_quotas` is imported inside the two `record_*_spend` coroutines
# below rather than here, and the reason is architectural rather than
# stylistic. `aida.sql_lineage_parser` imports this module to time a parse, and
# that module is documented (AT-D2) as catalog- and database-free: its tests
# run with no engine and nothing in it may reach the ORM. A module-level
# `from aida.usage_quotas import ...` would drag `aida.models` into the parser's
# import graph transitively, quietly making that guarantee false. The metric
# half of this module therefore imports nothing persistent, and the two
# functions that genuinely need a session pay for the import when they are
# called -- the same local-import idiom `aida.footprint_metrics` uses for
# `session_factory`.


class Parser(StrEnum):
    """Which parser spent the time. Closed, because it is a metric label.

    One value per parse entry point that a caller can reach, not one per
    internal helper: `view_lineage` is `sql_lineage_parser.parse_view_lineage`,
    `procedure_lineage` is `procedure_lineage.parse_procedure_lineage`, and
    `query_history` is the miner's sweep over captured statements.
    """

    VIEW_LINEAGE = "view_lineage"
    PROCEDURE_LINEAGE = "procedure_lineage"
    QUERY_HISTORY = "query_history"


class ParseOutcome(StrEnum):
    """How a parse ended. Closed, and the distinction matters operationally.

    `UNPARSEABLE` is normal: dynamic SQL, a construct sqlglot does not model, a
    definition the source truncated. The parsers are documented to degrade to an
    empty edge list with LOW confidence rather than raise, and an estate will
    always have some. `FAILED` is not normal -- it means sqlglot is unavailable
    or the parse raised -- and `UNSUPPORTED_DIALECT` means the source's dialect
    is outside the five the parsers support, which is a capability gap rather
    than a fault. Collapsing the three would make the only alertable one
    (`FAILED`) invisible inside the noise of the expected one.
    """

    PARSED = "PARSED"
    UNPARSEABLE = "UNPARSEABLE"
    UNSUPPORTED_DIALECT = "UNSUPPORTED_DIALECT"
    FAILED = "FAILED"


class SpendBasis(StrEnum):
    """Whether a token figure was billed by a provider or estimated here.

    The showback/chargeback line, as a metric label. See this module's
    docstring: these are never summed into one number.
    """

    REPORTED = "REPORTED"
    ESTIMATED = "ESTIMATED"


#: The dialects the lineage parsers support (`_SQLGLOT_DIALECT_MAP` in
#: `aida.sql_lineage_parser`), plus one bucket for everything else. Bounded on
#: purpose: `DataSource.dialect` is a validated column today, but a metric label
#: taken from a column is one schema change away from being unbounded, and this
#: series is high frequency.
KNOWN_DIALECTS: Final[frozenset[str]] = frozenset(
    {"postgres", "snowflake", "bigquery", "tsql", "oracle"}
)
OTHER_DIALECT: Final = "other"


def dialect_label(dialect: str) -> str:
    """`dialect` if it is one the parsers support, else `other`."""
    return dialect if dialect in KNOWN_DIALECTS else OTHER_DIALECT


PARSER_SECONDS = Histogram(
    "aida_parser_duration_seconds",
    (
        "Wall-clock time one lineage parse took. The parser's real cost: CPU on "
        "this platform's own workers, not a priced figure -- nothing here meters "
        "a dollar (see aida.cost_showback's COST_BASIS)."
    ),
    labelnames=("parser", "dialect"),
    # Sub-millisecond parses are common for a small view and multi-second ones
    # happen for a large procedure body, so the range spans four orders of
    # magnitude. Not tuned against a measurement -- no load test of the parse
    # path exists -- which is why the range is wide rather than narrow.
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
PARSER_STATEMENTS = Counter(
    "aida_parser_statements_total",
    (
        "SQL statements put through a lineage parser, by parser, dialect and "
        "closed-set outcome. FAILED is a deployment fault; UNPARSEABLE is the "
        "documented graceful degradation and is expected."
    ),
    labelnames=("parser", "dialect", "outcome"),
)
PARSER_INPUT_BYTES = Counter(
    "aida_parser_input_bytes_total",
    (
        "Bytes of value-free, literal-redacted SQL fed to a lineage parser. The "
        "volume half of parser cost, alongside the duration histogram."
    ),
    labelnames=("parser", "dialect"),
)

MODEL_CALLS = Counter(
    "aida_model_calls_total",
    (
        "Model gateway calls that returned an answer, by provider adapter and "
        "whether the provider reported what it billed. basis=ESTIMATED means "
        "the token figures for that call are this platform's own "
        "4-bytes-per-token estimate and are not billable."
    ),
    labelnames=("provider_type", "basis"),
)
MODEL_TOKENS = Counter(
    "aida_model_call_tokens_total",
    (
        "Model tokens, by provider adapter, direction and basis. REPORTED is "
        "what a provider said it billed; ESTIMATED is this platform's heuristic. "
        "Never summed into one figure -- they are different claims about the "
        "same call, and only one of them is money."
    ),
    labelnames=("provider_type", "direction", "basis"),
)
MODEL_PAYLOAD_BYTES = Counter(
    "aida_model_call_bytes_total",
    (
        "Serialized bytes sent to and received from a model route. The one "
        "volume figure that needs no estimate and no provider cooperation."
    ),
    labelnames=("provider_type", "direction"),
)
SPEND_ATTRIBUTION = Counter(
    "aida_model_spend_attribution_total",
    (
        "Model calls by whether their spend could be attributed to a datasource. "
        "attributed=false is not an error; it is the honest size of the gap "
        "between what the fleet-wide token counters see and what the per-source "
        "ledger can account for."
    ),
    labelnames=("attributed",),
)

_ATTRIBUTED: Final = "true"
_UNATTRIBUTED: Final = "false"
_INPUT: Final = "input"
_OUTPUT: Final = "output"


@dataclass(slots=True)
class ParseSpan:
    """One parse being measured. Handed to the caller by `parser_span`.

    The caller reports what the parse turned out to be by calling `observed`;
    a span nobody reports on is still timed and counted as `FAILED`, which is
    the right default -- the only way to leave a span unreported is to raise
    through it, and a parse that raised is exactly what `FAILED` means.
    """

    parser: Parser
    dialect: str
    statements: int = 1
    input_bytes: int = 0
    outcome: ParseOutcome = ParseOutcome.FAILED

    def observed(
        self,
        outcome: ParseOutcome,
        *,
        statements: int | None = None,
        input_bytes: int | None = None,
    ) -> None:
        """Record what this parse produced. Overrides the `FAILED` default."""
        self.outcome = outcome
        if statements is not None:
            self.statements = max(0, statements)
        if input_bytes is not None:
            self.input_bytes = max(0, input_bytes)


#: The two error strings that mean a *deployment* fault rather than a
#: definition the parser could not read. Matched as substrings because both
#: parsers embed them in a longer message, and named here once so the two entry
#: points cannot classify the same failure differently.
_FAILED_ERROR_MARKERS: Final = ("sqlglot library is not available",)
_UNSUPPORTED_DIALECT_MARKER: Final = "unsupported dialect"


def classify_parse(errors: Sequence[str], *, has_edges: bool) -> ParseOutcome:
    """Which `ParseOutcome` a parse result represents.

    Shared by `sql_lineage_parser.parse_view_lineage` and the procedure-parse
    call sites so the same failure is never counted two different ways. The
    ordering matters: a missing sqlglot and an unsupported dialect both produce
    zero edges, and collapsing either into `UNPARSEABLE` would bury a
    deployment fault inside the documented graceful degradation.
    """
    for error in errors:
        if any(marker in error for marker in _FAILED_ERROR_MARKERS):
            return ParseOutcome.FAILED
    for error in errors:
        if error.startswith(_UNSUPPORTED_DIALECT_MARKER):
            return ParseOutcome.UNSUPPORTED_DIALECT
    return ParseOutcome.PARSED if has_edges else ParseOutcome.UNPARSEABLE


@contextmanager
def parser_span(parser: Parser, *, dialect: str, sql: str | None = None) -> Iterator[ParseSpan]:
    """Time and count one parse, whatever it turns out to be.

    `sql` is used only for its length -- the text is never logged, hashed or
    labelled here, and by the time either parser sees it the literals have
    already been redacted by `_redact_literals`. Passing it saves the caller
    computing the byte count.

    The duration is observed in a `finally`, so a parse that raises is still
    timed; its outcome stays `FAILED` because nothing called `observed`.
    """
    label = dialect_label(dialect)
    span = ParseSpan(
        parser=parser,
        dialect=label,
        input_bytes=len(sql.encode()) if sql is not None else 0,
    )
    started = time.perf_counter()
    try:
        yield span
    finally:
        PARSER_SECONDS.labels(parser=parser.value, dialect=label).observe(
            time.perf_counter() - started
        )
        if span.statements > 0:
            PARSER_STATEMENTS.labels(
                parser=parser.value, dialect=label, outcome=span.outcome.value
            ).inc(span.statements)
        if span.input_bytes > 0:
            PARSER_INPUT_BYTES.labels(parser=parser.value, dialect=label).inc(span.input_bytes)


class ModelSpendEvidence(Protocol):
    """Structural stand-in for `ModelCallEvidence`, to keep the import one-way.

    `aida.model_gateway` imports this module, so this module must not import it
    back -- and `import-linter`'s contracts would object if it did. Only the
    seven members below are read, so they are declared as a `Protocol` and
    `ModelCallEvidence` satisfies it structurally without either module
    depending on the other's shape at runtime.

    They are read-only *properties* rather than plain attributes for a reason
    mypy is right to insist on: a plain `x: int` on a Protocol means a settable
    variable, and `ModelCallEvidence` is a frozen dataclass, so it cannot
    satisfy one. Read-only is also the truth about what this module wants --
    nothing here mutates a call's evidence, and a Protocol that asked for write
    access it never uses would exclude every immutable implementation.
    """

    @property
    def provider_type(self) -> str: ...

    @property
    def input_size_bytes(self) -> int: ...

    @property
    def output_size_bytes(self) -> int: ...

    @property
    def estimated_input_tokens(self) -> int: ...

    @property
    def estimated_output_tokens(self) -> int: ...

    @property
    def provider_input_tokens(self) -> int | None: ...

    @property
    def provider_output_tokens(self) -> int | None: ...


def observe_model_call(evidence: ModelSpendEvidence) -> None:
    """Count one completed model call's tokens and bytes. No database, no session.

    Called from `ProviderNeutralModelGateway.structured_completion` so that every
    caller of the gateway is counted once, in one place, without each of them
    having to remember -- and without this module needing anything the gateway
    does not already have in hand.

    A call whose provider reported usage contributes *both* bases: the reported
    figure because it is what was billed, and the estimate because it is what
    the input cap was checked against, and an operator comparing the two is how
    a drifting estimator gets noticed. They are separate label values, never a
    sum.
    """
    provider = evidence.provider_type
    reported_input = evidence.provider_input_tokens
    reported_output = evidence.provider_output_tokens
    has_report = reported_input is not None or reported_output is not None
    MODEL_CALLS.labels(
        provider_type=provider,
        basis=SpendBasis.REPORTED.value if has_report else SpendBasis.ESTIMATED.value,
    ).inc()
    for direction, size in (
        (_INPUT, evidence.input_size_bytes),
        (_OUTPUT, evidence.output_size_bytes),
    ):
        if size > 0:
            MODEL_PAYLOAD_BYTES.labels(provider_type=provider, direction=direction).inc(size)
    for direction, estimated in (
        (_INPUT, evidence.estimated_input_tokens),
        (_OUTPUT, evidence.estimated_output_tokens),
    ):
        if estimated > 0:
            MODEL_TOKENS.labels(
                provider_type=provider, direction=direction, basis=SpendBasis.ESTIMATED.value
            ).inc(estimated)
    for direction, reported in ((_INPUT, reported_input), (_OUTPUT, reported_output)):
        if reported is not None and reported > 0:
            MODEL_TOKENS.labels(
                provider_type=provider, direction=direction, basis=SpendBasis.REPORTED.value
            ).inc(reported)


def billable_tokens(evidence: ModelSpendEvidence) -> tuple[int, SpendBasis]:
    """The token figure to charge this call, and which basis it came from.

    Provider-reported input plus output where the provider reported either, and
    the estimate otherwise. The basis travels with the number so no caller can
    persist or quote it without also being able to say what it is -- the same
    discipline `AgentRun.budget_evidence.basis` already applies.

    A partially-reporting provider (input reported, output not) is charged on
    the reported parts plus the estimate for the missing direction, and the
    basis is still REPORTED, because the figure is no longer purely a guess.
    That mixture is the honest reading of a partial report; pretending the
    missing direction cost nothing would understate the bill.
    """
    reported_input = evidence.provider_input_tokens
    reported_output = evidence.provider_output_tokens
    if reported_input is None and reported_output is None:
        return (
            evidence.estimated_input_tokens + evidence.estimated_output_tokens,
            SpendBasis.ESTIMATED,
        )
    charged = (
        reported_input if reported_input is not None else evidence.estimated_input_tokens
    ) + (reported_output if reported_output is not None else evidence.estimated_output_tokens)
    return charged, SpendBasis.REPORTED


async def record_model_spend(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    evidence: ModelSpendEvidence,
) -> None:
    """Attribute one model call's tokens to a source, in the day's usage window.

    The per-source half of model cost. Accumulates into
    `source_usage_window` / `tenant_usage_window` under
    `UsageDimension.MODEL_TOKENS` -- rows, not labels, for the cardinality
    reason in this module's docstring.

    `datasource_id=None` is accepted and recorded as unattributed rather than
    refused: a call made on a path that has no single source (a marketplace
    question spanning a catalog, for instance) is still real spend, and dropping
    it would make the tenant total quietly wrong. The
    `aida_model_spend_attribution_total` counter is how an operator sees how
    much of the fleet's spend is in that state.

    Uses `record_usage`, not `consume_quota`: attributing spend that has already
    happened must never refuse it. Enforcement for this dimension happens
    *before* the call, wherever a caller chooses to enforce it.
    """
    charged, basis = billable_tokens(evidence)
    SPEND_ATTRIBUTION.labels(
        attributed=_ATTRIBUTED if datasource_id is not None else _UNATTRIBUTED
    ).inc()
    if charged <= 0:
        return
    from aida.usage_quotas import UsageDimension, record_usage

    await record_usage(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        dimension=UsageDimension.MODEL_TOKENS,
        amount=charged,
    )
    _log.info(
        "model_spend_recorded",
        organization_id=str(organization_id),
        datasource_id=str(datasource_id) if datasource_id else None,
        provider_type=evidence.provider_type,
        tokens=charged,
        basis=basis.value,
    )


async def record_parser_spend(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    statements: int,
) -> None:
    """Attribute parsed statements to a source, in the day's usage window.

    The per-source half of parser cost, under
    `UsageDimension.PARSER_STATEMENTS`. Statements rather than seconds, because
    seconds are a property of the machine that ran the parse and statements are
    a property of the source that asked for it -- the number that stays
    comparable between a laptop and a production worker, and the one a
    per-source quota can be written against.

    A call with nothing parsed is a no-op rather than an error: a declined parse
    (an unsupported dialect, an unreadable definition) consumed the parser's
    time, which the histogram already has, but attributing zero statements to
    the source is the truthful record.
    """
    if statements <= 0:
        return
    from aida.usage_quotas import UsageDimension, record_usage

    await record_usage(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        dimension=UsageDimension.PARSER_STATEMENTS,
        amount=statements,
    )
