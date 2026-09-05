#!/usr/bin/env python3
"""P3-09: backfill ``AssetCertification.evidence`` for pre-P3-09 ACTIVE rows.

Every certification created before P3-09 shipped has ``evidence IS NULL``.
The alembic migration does not touch historical rows -- certification
history is retained audit evidence and is never mutated by a schema
change. This CLI is the opt-in one-shot backfill: it walks ACTIVE
table-level rows with ``evidence IS NULL``, composes a *best-effort*
snapshot from today's description version / ownership / quality /
glossary state (the true state at certify time is gone), and stamps
``backfilled=True`` inside the JSON so downstream readers can distinguish
a reconstructed snapshot from an as-of-certify one.

Idempotent: the ``evidence IS NULL`` filter is the only writer -- a
second run touches nothing that a prior run already populated.

Usage:
    python scripts/backfill_certification_evidence.py               # apply
    python scripts/backfill_certification_evidence.py --dry-run     # count only

The default is dry-run OFF; pass ``--dry-run`` to see how many rows
would be touched without committing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from aida.certification_evidence import backfill_certification_evidence_v1
from aida.db import session_factory


async def _run(dry_run: bool) -> int:
    async with session_factory() as session:
        populated = await backfill_certification_evidence_v1(session)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return populated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be populated without committing.",
    )
    args = parser.parse_args()
    populated = asyncio.run(_run(args.dry_run))
    verb = "would populate" if args.dry_run else "populated"
    print(f"{verb} evidence on {populated} certification row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
