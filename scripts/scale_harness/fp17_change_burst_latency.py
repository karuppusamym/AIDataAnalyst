#!/usr/bin/env python3
"""R11-FP17 scale harness — does interactive read latency hold during a change burst?

The tracker row's remaining work names "a load test showing interactive latency
holds during a change burst". Nothing in `scripts/scale_harness/` drove a change
burst before this: `ct2_*` measures catalog pagination at depth, `gp1_*`
measures projector memory, `pr5_*` generates source tables. This script is the
missing one.

**The question it answers, stated so the answer cannot be overclaimed.** When a
rescan floods `metadata_change_signal` with pending rows, do the two reads a
person is waiting on stay as fast as they were? It measures that by taking a
baseline first, then re-measuring the *same* reads while a writer is inserting
signals as fast as the database will take them, and reporting both
distributions plus the delta. It does not simulate concurrent users, it does not
exercise HTTP, and it does not run the change-signal *processing* pass -- see
"What this is not" below, which is part of the result, not a disclaimer bolted
onto it.

**The two reads, and why these two.**

* `aida.footprint_gaps.footprint_gaps` -- the Operations screen's read, and the
  one directly exposed to the burst: it counts pending change signals and takes
  the age of the oldest. If a growing queue makes anything slow, this is where
  it shows first, and it is the read whose gauges
  `aida.footprint_metrics` publishes for alerting.
* `aida.api.list_tables` -- the catalog read a user hits, on the same
  organization, touching none of the burst's rows. It is the control: if this
  degrades too, the cause is database-wide contention (locks, WAL, autovacuum,
  connection pool) rather than anything about the queue's shape.

Both are called as the **real endpoint bodies**, in-process, against a real
Postgres-backed `AsyncSession` -- the same pattern and the same justification as
`ct2_measure_pagination.py`, which that file's docstring sets out.

**Percentiles, and why p99 is reported but not trusted.** p50/p95/p99 over the
sample window, and the raw sample count alongside them. At the default
`--samples 200` a p99 is three samples; it is printed because hiding it would
invite someone to compute it themselves from a smaller number, and it is
labelled low-confidence in the output because at that count it is one slow GC
pause away from meaningless. Raise `--samples` before quoting a p99.

**Self-contained, and it cleans up after itself.** It creates its own synthetic
organization (default slug `scale-harness-fp17`), and on exit deletes that
organization and everything FK-chained under it -- the same bottom-up,
`organization_id`-scoped delete `ct2_cleanup.py` does, in a `finally`, so a
crash mid-measurement still tidies. `--keep` leaves the scope for inspection and
`--cleanup-only` deletes a scope a previous `--keep` left behind. It never
touches a row outside its own organization.

**Requires a running stack, and will say so rather than start one.** It needs
the dev stack's `postgres` reachable at `Settings.database_url`, and
`AIDA_ENVIRONMENT` set in the shell (the same requirement every other script in
this directory has). It starts, stops, restarts and rebuilds nothing.

    # Phase A: the stack must already be up (`docker compose up -d postgres`)
    env AIDA_ENVIRONMENT=development \\
      .venv/Scripts/python.exe scripts/scale_harness/fp17_change_burst_latency.py \\
      --tables 500 --signals 5000 --samples 200

    # Tidy a scope a previous --keep run left behind
    env AIDA_ENVIRONMENT=development \\
      .venv/Scripts/python.exe scripts/scale_harness/fp17_change_burst_latency.py \\
      --cleanup-only

**What this is not.** Read this before quoting a number from it.

* **Not a measurement of the processing pass.** It floods the queue; it does not
  run `change_signal_processing`. `change_signal_processing_interval_minutes`
  ships at 0, so on a default deployment nothing drains this queue anyway, and
  measuring the reads against a queue that is not being drained is the
  worst-case shape -- which is the right one for a latency claim, and the wrong
  one for a throughput claim. There is no throughput claim here.
* **Not HTTP.** No server, no serialization, no auth middleware, no connection
  reuse across a network. It measures the database work an interactive read
  does, which is the part a change burst can plausibly affect, and excludes
  everything a real request also pays.
* **Not concurrent users.** One reader, one writer. A real burst arrives while
  many readers are already queued, and this harness will therefore report a
  *smaller* degradation than production would.
* **Not a capacity result.** Whatever it reports is one machine's number, at
  whatever scale the flags asked for, against whatever else that machine was
  doing. A number from a laptop is not a number from a deployment, and the
  alert thresholds in `infra/monitoring/` are left as documented placeholders
  precisely because this script has not been run against a real one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select

from aida.api import list_tables
from aida.change_signal_models import MetadataChangeSignal
from aida.footprint_gaps import footprint_gaps
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security_types import SecurityContext
from atlas.platform.config import get_settings
from atlas.platform.db import get_session_factory

DEFAULT_ORG_SLUG = "scale-harness-fp17"
DEFAULT_DATASOURCE_NAME = "scale-harness-fp17-datasource"
_FINGERPRINT = "fp17-change-burst"

#: The read roles `list_tables` requires and `footprint_gaps` reads through.
#: `Operations` is what the footprint register's own routes require, so the
#: harness sees exactly what an operator reading that screen would see.
_ROLES = frozenset({"Viewer", "Operations"})


# ---------------------------------------------------------------------------
# Measurement primitives
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Same implementation as
    `ct2_measure_pagination._percentile`, deliberately copied rather than
    imported: these scripts are run individually and a shared module between
    two harnesses is a dependency neither of them needs."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class Samples:
    """One read's latency distribution over one phase."""

    label: str
    values_ms: list[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.values_ms)

    def summary(self) -> dict[str, float | int]:
        if not self.values_ms:
            return {"samples": 0}
        return {
            "samples": self.count,
            "min_ms": round(min(self.values_ms), 3),
            "p50_ms": round(_percentile(self.values_ms, 50), 3),
            "p95_ms": round(_percentile(self.values_ms, 95), 3),
            "p99_ms": round(_percentile(self.values_ms, 99), 3),
            "max_ms": round(max(self.values_ms), 3),
            "mean_ms": round(statistics.fmean(self.values_ms), 3),
        }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    parser.add_argument("--datasource-name", default=DEFAULT_DATASOURCE_NAME)
    parser.add_argument(
        "--tables",
        type=int,
        default=500,
        help="MetadataTable rows the interactive catalog read pages over "
        "(default: 500 -- enough that list_tables does real work, small "
        "enough that setup is seconds; ct2_generate_catalog.py is the script "
        "for a 100K-table scope)",
    )
    parser.add_argument(
        "--signals",
        type=int,
        default=5_000,
        help="pending change signals the burst inserts (default: 5000)",
    )
    parser.add_argument(
        "--signal-batch-size",
        type=int,
        default=500,
        help="signals per bulk-insert round trip (default: 500). This is the "
        "burst's shape, not just its size: smaller batches mean more, shorter "
        "write transactions competing with the reads",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=200,
        help="latency samples per read per phase (default: 200). A p99 at this "
        "count rests on 3 samples and is reported as low-confidence; raise it "
        "before quoting one",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the synthetic organization in place instead of deleting it",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="delete the synthetic organization and exit, measuring nothing",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the result as JSON on stdout instead of a table",
    )
    return parser.parse_args(argv)


def _context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="fp17-change-burst-harness",
        principal_type="USER",
        organization_id=organization_id,
        roles=_ROLES,
    )


# ---------------------------------------------------------------------------
# Scope setup and teardown
# ---------------------------------------------------------------------------


async def _ensure_scope(
    session, *, org_slug: str, datasource_name: str, tables: int
) -> tuple[UUID, UUID]:
    """Create the synthetic org/lob/domain/project/datasource/catalog/schema
    hierarchy and `tables` tables under it. Returns (organization_id,
    datasource_id).

    Refuses if the slug already exists, for the reason `ct2_generate_catalog`
    gives: re-running against a populated scope would double the row counts and
    skew every timing in the report. `--cleanup-only` is the way out.
    """
    existing = await session.scalar(
        select(Organization).where(Organization.slug == org_slug)
    )
    if existing is not None:
        raise SystemExit(
            f"organization slug {org_slug!r} already exists (id={existing.id}). "
            f"Run this script with `--cleanup-only --org-slug {org_slug}` first, "
            f"or pass a different --org-slug."
        )

    org = Organization(id=uuid4(), name="FP-17 Change Burst Harness", slug=org_slug)
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Change Burst", code="FP17BURST"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Change Burst Domain",
        code="FP17BURST",
        is_default=True,
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Change Burst Project",
        slug=f"{org_slug}-project",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=datasource_name,
        connector_type="postgres",
        dialect="postgres",
        environment="DEV",
        network_zone="default",
        # Never resolved. This source exists to give the harness's rows a real
        # organization_id/datasource_id scope; no discovery ever runs against
        # it, and a syntactically valid but unresolvable reference documents
        # that rather than pointing at a real secret.
        credential_reference="env://AIDA_SCALE_HARNESS_FP17_UNUSED",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="fp17_burst_catalog",
        fingerprint=_FINGERPRINT,
    )
    for row in (org, lob, domain, project, datasource, catalog):
        session.add(row)
        await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint=_FINGERPRINT,
    )
    session.add(schema)
    await session.flush()

    table_rows = [
        {
            "id": uuid4(),
            "organization_id": org.id,
            "datasource_id": datasource.id,
            "schema_id": schema.id,
            "name": f"burst_table_{index:06d}",
            "object_type": "TABLE",
            "status": "ACTIVE",
            "fingerprint": f"{_FINGERPRINT}-{index}",
        }
        for index in range(tables)
    ]
    if table_rows:
        await session.execute(insert(MetadataTable), table_rows)
    await session.commit()
    return org.id, datasource.id


async def _cleanup(session, org_slug: str) -> None:
    """Delete the synthetic organization and everything under it, bottom-up.

    Every table here carries `organization_id`, so each stage is a plain
    `DELETE ... WHERE organization_id = :org_id` -- the same shape and the same
    scoping guarantee as `ct2_cleanup.py`. Never a bulk delete over the whole
    catalog.
    """
    org = await session.scalar(
        select(Organization).where(Organization.slug == org_slug)
    )
    if org is None:
        print(f"no organization with slug {org_slug!r} -- nothing to clean up")
        return
    org_id = org.id
    for label, statement in [
        (
            "change_signals",
            delete(MetadataChangeSignal).where(MetadataChangeSignal.organization_id == org_id),
        ),
        ("columns", delete(MetadataColumn).where(MetadataColumn.organization_id == org_id)),
        ("tables", delete(MetadataTable).where(MetadataTable.organization_id == org_id)),
        ("schemas", delete(MetadataSchema).where(MetadataSchema.organization_id == org_id)),
        ("catalogs", delete(MetadataCatalog).where(MetadataCatalog.organization_id == org_id)),
        ("datasources", delete(DataSource).where(DataSource.organization_id == org_id)),
        ("projects", delete(Project).where(Project.organization_id == org_id)),
        ("data_domains", delete(DataDomain).where(DataDomain.organization_id == org_id)),
        (
            "lines_of_business",
            delete(LineOfBusiness).where(LineOfBusiness.organization_id == org_id),
        ),
        ("organization", delete(Organization).where(Organization.id == org_id)),
    ]:
        result = await session.execute(statement)
        await session.commit()
        print(f"  {label}: {result.rowcount} row(s) deleted")


# ---------------------------------------------------------------------------
# The burst, and the reads measured against it
# ---------------------------------------------------------------------------


def _signal_rows(
    *, organization_id: UUID, datasource_id: UUID, table_ids: list[UUID], count: int, now: datetime
) -> list[dict[str, object]]:
    """`count` PENDING STRUCTURAL signals, spread backwards in time.

    `detected_at` is staggered so the oldest is genuinely older than the
    newest: `footprint_gaps` reports the age of the oldest pending signal, and
    a burst whose rows all share one timestamp would make that figure constant
    and hide the thing the read actually computes.
    """
    return [
        {
            "id": uuid4(),
            "organization_id": organization_id,
            "datasource_id": datasource_id,
            "subject_kind": "TABLE",
            "subject_id": table_ids[index % len(table_ids)],
            "signal_type": "STRUCTURE_CHANGED",
            "change_class": "COLUMNS_ADDED",
            "status": "PENDING",
            "detected_at": now - timedelta(seconds=count - index),
            "outcome": {},
        }
        for index in range(count)
    ]


async def _sample_reads(
    session_factory,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    samples: int,
    gaps: Samples,
    catalog: Samples,
    stop: asyncio.Event | None = None,
) -> None:
    """Time both interactive reads, `samples` times each, alternating.

    A fresh session per sample, deliberately: a long-lived session would hold
    one connection and one snapshot for the whole phase, which is the opposite
    of what an interactive read does and would hide exactly the pool and
    visibility effects a concurrent writer causes.
    """
    settings = get_settings()
    context = _context(organization_id)
    for _ in range(samples):
        if stop is not None and stop.is_set():
            return
        async with session_factory() as session:
            started = time.perf_counter()
            await footprint_gaps(
                session,
                context=context,
                settings=settings,
                organization_id=organization_id,
            )
            gaps.values_ms.append((time.perf_counter() - started) * 1000)
        async with session_factory() as session:
            started = time.perf_counter()
            await list_tables(
                datasource_id,
                q=None,
                object_type=None,
                table_status="ACTIVE",
                limit=100,
                offset=0,
                cursor=None,
                context=context,
                session=session,
                settings=settings,
            )
            catalog.values_ms.append((time.perf_counter() - started) * 1000)


async def _drive_burst(
    session_factory,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    table_ids: list[UUID],
    signals: int,
    batch_size: int,
    done: asyncio.Event,
) -> int:
    """Insert `signals` pending change signals as fast as the database takes them.

    One transaction per batch rather than one for the whole burst: a single
    enormous transaction would make every row invisible to the reader until it
    committed, so the reads would see a queue of zero for the entire phase and
    then a queue of `signals` after it -- measuring nothing. Batches are what a
    real rescan produces and what the reader can actually see growing.
    """
    written = 0
    now = datetime.now(UTC)
    try:
        for start in range(0, signals, batch_size):
            rows = _signal_rows(
                organization_id=organization_id,
                datasource_id=datasource_id,
                table_ids=table_ids,
                count=min(batch_size, signals - start),
                now=now,
            )
            async with session_factory() as session:
                await session.execute(insert(MetadataChangeSignal), rows)
                await session.commit()
            written += len(rows)
    finally:
        done.set()
    return written


async def _run(args: argparse.Namespace) -> int:
    session_factory = get_session_factory()

    if args.cleanup_only:
        async with session_factory() as session:
            print(f"deleting organization slug={args.org_slug!r} and everything under it...")
            await _cleanup(session, args.org_slug)
        print("cleanup complete")
        return 0

    report: dict[str, object] = {
        "org_slug": args.org_slug,
        "tables": args.tables,
        "signals": args.signals,
        "signal_batch_size": args.signal_batch_size,
        "samples_per_phase": args.samples,
    }
    try:
        async with session_factory() as session:
            print(f"creating scope slug={args.org_slug!r} with {args.tables} tables...")
            organization_id, datasource_id = await _ensure_scope(
                session,
                org_slug=args.org_slug,
                datasource_name=args.datasource_name,
                tables=args.tables,
            )
        async with session_factory() as session:
            table_ids = list(
                (
                    await session.scalars(
                        select(MetadataTable.id).where(
                            MetadataTable.organization_id == organization_id
                        )
                    )
                ).all()
            )
        if not table_ids:
            raise SystemExit("no tables were created; nothing to attach change signals to")

        # Phase 1: baseline, no writer running.
        print(f"phase 1/2: baseline, {args.samples} samples per read, empty queue...")
        baseline_gaps = Samples("footprint_gaps")
        baseline_catalog = Samples("list_tables")
        baseline_started = time.perf_counter()
        await _sample_reads(
            session_factory,
            organization_id=organization_id,
            datasource_id=datasource_id,
            samples=args.samples,
            gaps=baseline_gaps,
            catalog=baseline_catalog,
        )
        baseline_seconds = time.perf_counter() - baseline_started

        # Phase 2: the same reads, while the writer floods the queue. The reader
        # is capped at the same sample count, and the writer sets `done` when it
        # finishes -- whichever ends first, the other is stopped, and the report
        # says which so a phase that was not actually concurrent is visible
        # rather than quoted as though it were.
        print(
            f"phase 2/2: burst of {args.signals} signals in batches of "
            f"{args.signal_batch_size}, sampling concurrently..."
        )
        burst_gaps = Samples("footprint_gaps")
        burst_catalog = Samples("list_tables")
        done = asyncio.Event()
        burst_started = time.perf_counter()
        writer = asyncio.create_task(
            _drive_burst(
                session_factory,
                organization_id=organization_id,
                datasource_id=datasource_id,
                table_ids=table_ids,
                signals=args.signals,
                batch_size=args.signal_batch_size,
                done=done,
            )
        )
        reader = asyncio.create_task(
            _sample_reads(
                session_factory,
                organization_id=organization_id,
                datasource_id=datasource_id,
                samples=args.samples,
                gaps=burst_gaps,
                catalog=burst_catalog,
                stop=done,
            )
        )
        written = await writer
        await reader
        burst_seconds = time.perf_counter() - burst_started

        async with session_factory() as session:
            pending = await session.scalar(
                select(MetadataChangeSignal.id)
                .where(MetadataChangeSignal.organization_id == organization_id)
                .limit(1)
            )
        report.update(
            {
                "signals_written": written,
                "queue_non_empty_at_end": pending is not None,
                "baseline_seconds": round(baseline_seconds, 3),
                "burst_seconds": round(burst_seconds, 3),
                "writer_finished_before_reader": burst_gaps.count < args.samples,
                "baseline": {
                    baseline_gaps.label: baseline_gaps.summary(),
                    baseline_catalog.label: baseline_catalog.summary(),
                },
                "during_burst": {
                    burst_gaps.label: burst_gaps.summary(),
                    burst_catalog.label: burst_catalog.summary(),
                },
            }
        )
        _render(report, as_json=args.json)
        return 0
    finally:
        if args.keep:
            print(
                f"--keep given: leaving organization slug={args.org_slug!r} in place. "
                f"Delete it with `--cleanup-only --org-slug {args.org_slug}`."
            )
        else:
            async with session_factory() as session:
                print("cleaning up...")
                await _cleanup(session, args.org_slug)


def _render(report: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    baseline = report["baseline"]
    burst = report["during_burst"]
    assert isinstance(baseline, dict)
    assert isinstance(burst, dict)
    print()
    print("=" * 78)
    print("R11-FP17 change-burst interactive latency")
    print("=" * 78)
    print(
        f"tables={report['tables']}  signals_written={report.get('signals_written')}  "
        f"batch={report['signal_batch_size']}  samples/phase={report['samples_per_phase']}"
    )
    if report.get("writer_finished_before_reader"):
        print(
            "NOTE: the writer finished before the reader reached its sample count, so the "
            "burst-phase samples below cover a SHORTER window than the baseline. Raise "
            "--signals or lower --samples for a fully concurrent phase."
        )
    print()
    header = (
        f"{'read':<18}{'phase':<10}{'n':>6}"
        f"{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}"
    )
    print(header)
    print("-" * len(header))
    for phase, block in (("baseline", baseline), ("burst", burst)):
        for label, summary in block.items():
            if not isinstance(summary, dict) or not summary.get("samples"):
                print(f"{label:<18}{phase:<10}{'0':>6}  (no samples)")
                continue
            print(
                f"{label:<18}{phase:<10}{summary['samples']:>6}"
                f"{summary['p50_ms']:>10}{summary['p95_ms']:>10}"
                f"{summary['p99_ms']:>10}{summary['max_ms']:>10}"
            )
    print()
    for label in baseline:
        before = baseline[label]
        after = burst.get(label)
        if not (isinstance(before, dict) and isinstance(after, dict)):
            continue
        if not (before.get("samples") and after.get("samples")):
            continue
        p50_before, p50_after = float(before["p50_ms"]), float(after["p50_ms"])
        p95_before, p95_after = float(before["p95_ms"]), float(after["p95_ms"])
        print(
            f"{label}: p50 {p50_before:.2f} -> {p50_after:.2f} ms "
            f"({_ratio(p50_before, p50_after)}), "
            f"p95 {p95_before:.2f} -> {p95_after:.2f} ms ({_ratio(p95_before, p95_after)})"
        )
    print()
    print(
        "p99 at the default sample count rests on ~3 samples and is low confidence; "
        "raise --samples before quoting one. This is one machine's number against a "
        "queue nothing is draining -- see this script's docstring for what it is not."
    )


def _ratio(before: float, after: float) -> str:
    if before <= 0:
        return "n/a"
    return f"{after / before:.2f}x"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
