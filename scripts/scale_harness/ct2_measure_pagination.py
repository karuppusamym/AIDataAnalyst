#!/usr/bin/env python3
"""CT-2 scale harness — measure list_tables/list_columns latency by page depth.

Calls the REAL endpoint bodies (`aida.api.list_tables`, `aida.api.list_columns`)
in-process against a real Postgres-backed `AsyncSession` — the same pattern
`tests/test_catalog_pagination.py` uses (see that file's own docstring for why
this counts as exercising genuine query execution, not a mock), just pointed
at the real dev-stack Postgres instead of an in-memory sqlite engine, and at
the ~100K-table scope `ct2_generate_catalog.py` populated instead of a
handful of test rows.

What this proves: whether `list_tables`' keyset (cursor) branch's latency
stays flat as page depth increases, versus the plain `OFFSET` branch's cost
growing with depth — the CT-2 exit condition's actual claim. It does this by:

  1. Walking the keyset cursor chain sequentially from page 1 up to the
     deepest requested `--depths` value (there is no way to jump into a
     keyset walk at an arbitrary page — that lack of random access is
     structural to why it's fast), timing every single page call along the
     way, and reporting p50/p95 latency over a trailing window of hops
     ending at each requested depth.
  2. At each of those same depths, separately calling `list_tables` with
     `cursor=None` and `offset=(depth-1)*page_size` — the plain OFFSET
     branch — so the OFFSET-cost-grows-with-depth claim can be read directly
     off the same table, same page size, same run.

`list_columns` is NOT put through the same page-depth story: it paginates
*within one table's own columns* (`table_id` is an equality filter, not an
`ORDER BY` walked across the whole organization — see `src/aida/api.py`'s
`list_columns`), and this harness's tables carry ~15-35 columns each, so
"page depth" for columns never gets deep enough for OFFSET-vs-keyset cost to
diverge in the first place. What this script instead does for `list_columns`
is a plain correctness/latency sanity check: walk one real table's full
column list via cursor and confirm it completes, fast, in as many pages as
its column count implies. See README.md for the fuller explanation of why
this is not a gap in the proof, just an honest description of what "depth"
means for each endpoint.

Requires `AIDA_ENVIRONMENT` set in the shell (see ct2_generate_catalog.py's
docstring for why) and `ct2_generate_catalog.py` to have already populated
the `--org-slug` scope this script reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select

from aida.api import list_columns, list_tables
from aida.models import DataSource, MetadataTable, Organization
from aida.security_types import SecurityContext
from atlas.platform.config import get_settings
from atlas.platform.db import get_session_factory

DEFAULT_ORG_SLUG = "scale-harness-ct2"
DEFAULT_DATASOURCE_NAME = "scale-harness-ct2-datasource"


@dataclass
class HopTiming:
    page_number: int  # 1-indexed
    elapsed_ms: float
    row_count: int


@dataclass
class DepthResult:
    depth: int
    keyset_hop_ms: float | None  # latency of the single call that reached this depth
    keyset_window_p50_ms: float | None
    keyset_window_p95_ms: float | None
    offset_repeats_ms: list[float] = field(default_factory=list)

    @property
    def offset_p50_ms(self) -> float | None:
        return _percentile(self.offset_repeats_ms, 50) if self.offset_repeats_ms else None


def _percentile(values: list[float], pct: float) -> float:
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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    parser.add_argument("--datasource-name", default=DEFAULT_DATASOURCE_NAME)
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="`limit` passed to list_tables (default: 100, the endpoint's own "
        "default). Reaching a literal page-50000 depth at 100000 tables needs "
        "a smaller page size (e.g. --page-size 2); reaching it with the "
        "default page size needs more tables than this harness's 100000-table "
        "proxy scope has -- see README.md's honesty caveat.",
    )
    parser.add_argument(
        "--depths",
        default="1,1000,LAST",
        help="comma-separated 1-indexed page numbers to report on, or LAST for "
        "the deepest full page this org's table count actually has at "
        "--page-size (default: 1,1000,LAST)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=20,
        help="trailing hops ending at each depth to compute keyset p50/p95 "
        "over, to smooth single-call noise (default: 20)",
    )
    parser.add_argument(
        "--offset-repeats",
        type=int,
        default=3,
        help="how many times to repeat the OFFSET-branch call at each depth, "
        "reporting the median (default: 3)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="optional path to also write the raw results as JSON",
    )
    return parser.parse_args(argv)


def _resolve_depths(spec: str, *, max_page: int) -> list[int]:
    depths: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token.upper() == "LAST":
            depths.append(max_page)
        else:
            depths.append(int(token))
    depths = sorted({d for d in depths if 1 <= d <= max_page})
    if not depths:
        raise SystemExit(
            f"no requested depth falls within [1, {max_page}] (this org's table "
            f"count / --page-size). Lower --depths or --page-size."
        )
    return depths


async def _load_scope(
    session, *, org_slug: str, datasource_name: str
) -> tuple[Organization, DataSource, int]:
    org = await session.scalar(select(Organization).where(Organization.slug == org_slug))
    if org is None:
        raise SystemExit(
            f"no organization with slug {org_slug!r} -- run ct2_generate_catalog.py first"
        )
    datasource = await session.scalar(
        select(DataSource).where(
            DataSource.organization_id == org.id, DataSource.name == datasource_name
        )
    )
    if datasource is None:
        raise SystemExit(
            f"organization {org_slug!r} exists but has no datasource named "
            f"{datasource_name!r} -- check --datasource-name"
        )
    total_tables = (
        await session.scalar(
            select(func.count())
            .select_from(MetadataTable)
            .where(MetadataTable.datasource_id == datasource.id)
        )
        or 0
    )
    return org, datasource, total_tables


def _context(org_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="ct2-scale-harness",
        principal_type="USER",
        organization_id=org_id,
        roles=frozenset({"Viewer"}),
    )


async def _measure_list_tables(
    session,
    *,
    datasource: DataSource,
    context: SecurityContext,
    settings,
    page_size: int,
    depths: list[int],
    window: int,
    offset_repeats: int,
) -> list[DepthResult]:
    max_depth = max(depths)
    hops: list[HopTiming] = []
    cursor: str | None = None
    print(f"walking list_tables keyset chain to page {max_depth} (page_size={page_size})...")
    for page_number in range(1, max_depth + 1):
        started = time.perf_counter()
        page = await list_tables(
            datasource.id,
            q=None,
            object_type=None,
            table_status="ACTIVE",
            limit=page_size,
            offset=0,
            cursor=cursor,
            context=context,
            session=session,
            settings=settings,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        hops.append(
            HopTiming(page_number=page_number, elapsed_ms=elapsed_ms, row_count=len(page.items))
        )
        if page_number in depths or page_number % 5_000 == 0:
            print(f"  page {page_number:>7}: {elapsed_ms:8.2f} ms ({len(page.items)} rows)")
        if page.next_cursor is None:
            if page_number < max_depth:
                print(
                    f"  ran out of rows at page {page_number} "
                    f"(fewer tables than --depths requested) -- stopping walk early"
                )
            break
        cursor = page.next_cursor

    results: list[DepthResult] = []
    for depth in depths:
        window_hops = [h for h in hops if depth - window < h.page_number <= depth]
        exact = next((h for h in hops if h.page_number == depth), None)
        if exact is None:
            continue  # walk ended early, before this depth
        window_values = [h.elapsed_ms for h in window_hops]
        result = DepthResult(
            depth=depth,
            keyset_hop_ms=exact.elapsed_ms,
            keyset_window_p50_ms=_percentile(window_values, 50),
            keyset_window_p95_ms=_percentile(window_values, 95),
        )
        for _ in range(offset_repeats):
            started = time.perf_counter()
            await list_tables(
                datasource.id,
                q=None,
                object_type=None,
                table_status="ACTIVE",
                limit=page_size,
                offset=(depth - 1) * page_size,
                cursor=None,
                context=context,
                session=session,
                settings=settings,
            )
            result.offset_repeats_ms.append((time.perf_counter() - started) * 1000)
        results.append(result)
    return results


async def _measure_list_columns(
    session, *, datasource: DataSource, context: SecurityContext, settings, page_size: int
) -> dict:
    first_page = await list_tables(
        datasource.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        limit=1,
        offset=0,
        cursor=None,
        context=context,
        session=session,
        settings=settings,
    )
    if not first_page.items:
        return {"note": "no tables found; skipped list_columns check"}
    table_id = first_page.items[0].id
    hop_timings: list[float] = []
    total_columns = 0
    cursor: str | None = None
    while True:
        started = time.perf_counter()
        page = await list_columns(
            table_id,
            limit=page_size,
            offset=0,
            cursor=cursor,
            context=context,
            session=session,
            settings=settings,
        )
        hop_timings.append((time.perf_counter() - started) * 1000)
        total_columns += len(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return {
        "table_id": str(table_id),
        "total_columns": total_columns,
        "pages": len(hop_timings),
        "p50_ms": _percentile(hop_timings, 50),
        "p95_ms": _percentile(hop_timings, 95),
    }


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        org, datasource, total_tables = await _load_scope(
            session, org_slug=args.org_slug, datasource_name=args.datasource_name
        )
        max_page = max(1, -(-total_tables // args.page_size))  # ceil division
        depths = _resolve_depths(args.depths, max_page=max_page)
        print(
            f"organization={org.id} datasource={datasource.id} "
            f"total_tables={total_tables} page_size={args.page_size} "
            f"depths={depths} (max available page: {max_page})"
        )
        context = _context(org.id)

        table_results = await _measure_list_tables(
            session,
            datasource=datasource,
            context=context,
            settings=settings,
            page_size=args.page_size,
            depths=depths,
            window=args.window,
            offset_repeats=args.offset_repeats,
        )

        column_result = await _measure_list_columns(
            session, datasource=datasource, context=context, settings=settings, page_size=200
        )

    print("\n=== list_tables: keyset (cursor) vs. OFFSET latency by page depth ===")
    print(f"{'depth':>8}  {'keyset p50':>12}  {'keyset p95':>12}  {'offset p50':>12}")
    for r in table_results:
        offset_p50 = f"{r.offset_p50_ms:9.2f} ms" if r.offset_p50_ms is not None else "        n/a"
        print(
            f"{r.depth:>8}  {r.keyset_window_p50_ms:9.2f} ms  "
            f"{r.keyset_window_p95_ms:9.2f} ms  {offset_p50}"
        )
    print(
        "\nA flat keyset column (no upward trend as depth grows) against a "
        "growing offset column is the CT-2 exit condition's claim, read "
        "directly off real Postgres at this run's table count."
    )

    print("\n=== list_columns: single-table cursor walk (sanity check, not a depth story) ===")
    print(json.dumps(column_result, indent=2))

    if args.json_out:
        payload = {
            "organization_id": str(org.id),
            "datasource_id": str(datasource.id),
            "total_tables": total_tables,
            "page_size": args.page_size,
            "list_tables": [
                {
                    "depth": r.depth,
                    "keyset_hop_ms": r.keyset_hop_ms,
                    "keyset_window_p50_ms": r.keyset_window_p50_ms,
                    "keyset_window_p95_ms": r.keyset_window_p95_ms,
                    "offset_repeats_ms": r.offset_repeats_ms,
                    "offset_p50_ms": r.offset_p50_ms,
                }
                for r in table_results
            ],
            "list_columns": column_result,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:  # noqa: ASYNC230
            json.dump(payload, fh, indent=2)
        print(f"\nraw results written to {args.json_out}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
