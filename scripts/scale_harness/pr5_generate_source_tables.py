#!/usr/bin/env python3
"""PR-5 scale harness — stand up a large-table-count PostgreSQL *source* for
`DatasourceDiscoveryWorkflow` to discover and profile for real.

This is deliberately NOT Aida's own catalog DB (that's CT-2's
`ct2_generate_catalog.py`, a different script writing to a different
database). This script targets a **separate, standalone** Postgres container
(see `compose.pr5-source.yml` in this directory) that plays the role CN-3's
`tests/fixtures/postgres_versions/` fixture plays for connector-version
testing: a real, connectable Postgres a real `PostgresConnector` can run
`test_connection()` / `discover()` against — just optimized for table
*count* (100,000 minimal tables) instead of feature coverage.

Why raw asyncpg instead of a single `DO $$ ... $$` PL/pgSQL block (the other
option the harness plan considered): a `DO` block's dynamic `EXECUTE format(...)`
loop still issues one `CREATE TABLE` per iteration server-side, gains nothing
from being inside PL/pgSQL, and — worse for a 100K-table run — runs as one
giant, no-progress, unresumable transaction: it either commits 100,000 tables
atomically at the very end or rolls back everything on any failure, with no
visibility while it runs. This script instead batches many `CREATE TABLE`
statements into ONE semicolon-joined string per round trip and sends each
batch with `asyncpg.Connection.execute()`. asyncpg's simple query protocol
executes a multi-statement string as one wire round trip, and per the
Postgres wire protocol, an unadorned multi-statement simple-query message is
itself an implicit transaction (all-or-nothing) -- so each batch (default
500 tables) is both a single network round trip AND a natural checkpoint:
a crash mid-run loses at most one in-flight batch, and progress prints after
every batch.

Run this against the standalone container from `compose.pr5-source.yml`, NOT
the dev stack's main `postgres` service.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import asyncpg

DEFAULT_DSN = "postgresql://source:source-local-only@localhost:55435/bank_demo_scale"
DEFAULT_SCHEMA = "public"
DEFAULT_TABLE_PREFIX = "scale_t_"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help=f"asyncpg DSN for the standalone source container (default: {DEFAULT_DSN})",
    )
    parser.add_argument(
        "--tables",
        type=int,
        default=100_000,
        help="number of minimal tables to create (default: 100000, matching "
        "this harness's proxy scope — see README.md)",
    )
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--table-prefix", default=DEFAULT_TABLE_PREFIX)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="CREATE TABLE statements per round trip / implicit transaction "
        "(default: 500)",
    )
    parser.add_argument(
        "--no-drop",
        action="store_true",
        help="skip dropping and recreating --schema first (default: drop it, "
        "so re-running this script always starts from a clean, exact "
        "--tables count instead of accumulating leftover tables from a "
        "prior run with a different --tables value)",
    )
    return parser.parse_args(argv)


def _table_ddl(schema: str, prefix: str, index: int, width: int) -> str:
    name = f"{prefix}{index:0{width}d}"
    # Three columns, deliberately minimal (PR-5 is a table-*count* proof, not
    # a per-table-complexity one — CN-3's fixture already covers feature
    # breadth). A primary key and a default-bearing column keep this a
    # structurally normal table a profiler would actually encounter, not a
    # degenerate single-column stub.
    return (
        f'CREATE TABLE "{schema}"."{name}" ('
        f"id integer PRIMARY KEY, "
        f"label text, "
        f"created_at timestamptz NOT NULL DEFAULT now()"
        f");"
    )


async def _run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(args.dsn)
    try:
        if not args.no_drop:
            print(f"dropping and recreating schema {args.schema!r}...")
            await conn.execute(f'DROP SCHEMA IF EXISTS "{args.schema}" CASCADE;')
            await conn.execute(f'CREATE SCHEMA "{args.schema}";')

        width = len(str(args.tables))
        started = time.monotonic()
        created = 0
        for batch_start in range(0, args.tables, args.batch_size):
            batch_end = min(batch_start + args.batch_size, args.tables)
            statements = [
                _table_ddl(args.schema, args.table_prefix, i, width)
                for i in range(batch_start, batch_end)
            ]
            await conn.execute("\n".join(statements))
            created = batch_end
            elapsed = time.monotonic() - started
            rate = created / elapsed if elapsed > 0 else 0.0
            print(
                f"  {created:>7}/{args.tables} tables "
                f"— {elapsed:6.1f}s elapsed, {rate:6.0f} tables/s",
                flush=True,
            )

        elapsed = time.monotonic() - started
        print(f"done: {created} tables in schema {args.schema!r} in {elapsed:.1f}s")
        print(
            "Next: follow README.md's Phase B/C instructions to register this "
            "as a real DataSource and trigger DatasourceDiscoveryWorkflow "
            "against it through the app's real API."
        )
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (asyncpg.PostgresError, OSError, ConnectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Is the standalone source container up? "
            "docker compose -f compose.pr5-source.yml up -d",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
