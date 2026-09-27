#!/usr/bin/env python3
"""Walk one ontology version through its whole lifecycle on a running deployment.

R11-C1 asked for exactly one thing the in-process tests cannot give: the
lifecycle -- draft, independent review, publication -- executed **through the
running application**. `tests/test_ontology_api.py` proves the rules by calling
the handlers directly over SQLite; it says nothing about whether the deployed
routes, the real role dependencies and the real Postgres agree.

This drives the live HTTP API, in order, with two identities:

    POST /v1/organizations/{org}/ontology-versions   (author creates a DRAFT)
    POST /v1/ontology-versions/{id}/submit           (author submits)
    POST /v1/governance/reviews/{id}/decision        (author tries to APPROVE -- must refuse)
    POST /v1/governance/reviews/{id}/decision        (a different steward APPROVEs)
    GET  /v1/organizations/{org}/ontology-versions   (the version reads back published)

**It writes.** Each run publishes a new version of the `atlas_e2e` ontology,
which is why this is its own script and not a section of
`verify_end_to_end.py`: that one is safe to run on every deploy, and a verifier
that accumulated governed content every time it ran would not be.

It is re-runnable: an ontology version must name the version it builds on and
keep every published concept key, so each run reads the head, sets
`base_version` from it, and carries the prior concepts forward.

Usage:
    python scripts/verify_ontology_lifecycle.py
    python scripts/verify_ontology_lifecycle.py --org sample-bank --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Run as `python scripts/verify_ontology_lifecycle.py`, the repository root is
# not on `sys.path`, and the shared HTTP helper lives in a sibling script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.verify_end_to_end import Api, Report, resolve_org  # noqa: E402

#: Ontology and concept keys must match `^[a-z][a-z0-9_]{0,99}$` -- lowercase
#: and underscores. The first live run used hyphens and the API refused it,
#: which is the route validating correctly.
ONTOLOGY_KEY = "atlas_e2e"
AUTHOR = "e2e-ontology-author"
REVIEWER = "e2e-ontology-reviewer"
STEWARD_ROLES = "DataSteward"


def _definition(concept_keys: list[str]) -> dict[str, Any]:
    return {
        "name": "Atlas end-to-end verification ontology",
        "owner": "data-governance",
        "provenance": (
            "Published by scripts/verify_ontology_lifecycle.py to prove the "
            "lifecycle on a running deployment."
        ),
        "concepts": [
            {
                "key": key,
                "name": key.replace("_", " ").title(),
                "description": f"The {key} concept, carried forward on every run.",
            }
            for key in concept_keys
        ],
    }


def _published_head(api: Api, org_id: str) -> tuple[int, list[str]]:
    """The published version number and its concept keys, or (0, [])."""
    status, versions = api.call(
        "GET",
        f"/v1/organizations/{org_id}/ontology-versions?limit=100",
        principal=AUTHOR,
        roles=STEWARD_ROLES,
        org_id=org_id,
    )
    if status != 200 or not isinstance(versions, list):
        return 0, []
    published = [
        v
        for v in versions
        if v.get("ontology_key") == ONTOLOGY_KEY and v.get("status") == "APPROVED"
    ]
    if not published:
        return 0, []
    head = max(published, key=lambda v: int(v.get("version", 0)))
    keys = [c["key"] for c in (head.get("definition") or {}).get("concepts", [])]
    return int(head["version"]), keys


def run(api: Api, report: Report, org_slug: str) -> None:
    org_id = resolve_org(api, report, org_slug)
    if org_id is None:
        return

    base_version, prior_keys = _published_head(api, org_id)
    run_key = "run_" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    concept_keys = [*prior_keys, run_key] if prior_keys else ["customer", run_key]
    report.record(
        "the published head is readable",
        "PASS",
        f"base_version={base_version}, carrying {len(prior_keys)} concept(s) forward",
    )

    status, created = api.call(
        "POST",
        f"/v1/organizations/{org_id}/ontology-versions",
        body={
            "ontology_key": ONTOLOGY_KEY,
            "base_version": base_version,
            "definition": _definition(concept_keys),
        },
        principal=AUTHOR,
        roles=STEWARD_ROLES,
        org_id=org_id,
    )
    if status not in (200, 201) or not isinstance(created, dict):
        report.record("the author creates a draft", "FAIL", f"HTTP {status}: {created}")
        return
    version_id = created["id"]
    report.record(
        "the author creates a draft",
        "PASS" if created.get("status") == "DRAFT" else "FAIL",
        f"version {created.get('version')} status={created.get('status')}",
    )

    status, submitted = api.call(
        "POST",
        f"/v1/ontology-versions/{version_id}/submit",
        principal=AUTHOR,
        roles=STEWARD_ROLES,
        org_id=org_id,
    )
    review_id = submitted.get("governance_review_id") if isinstance(submitted, dict) else None
    if status != 200 or not review_id:
        report.record("the author submits for review", "FAIL", f"HTTP {status}: {submitted}")
        return
    report.record(
        "the author submits for review",
        "PASS",
        f"status={submitted.get('status')}, review {review_id}",
    )

    # INV-8. The refusal is the control, so it is asserted rather than assumed:
    # a route that let the author approve their own ontology would still end
    # this script with a published version, and only this check would notice.
    status, refused = api.call(
        "POST",
        f"/v1/governance/reviews/{review_id}/decision",
        body={"decision": "APPROVE", "reason": "approving my own ontology"},
        principal=AUTHOR,
        roles=STEWARD_ROLES,
        org_id=org_id,
    )
    report.record(
        "the author cannot approve their own ontology",
        "PASS" if status in (403, 409) else "FAIL",
        f"HTTP {status}",
    )

    status, decided = api.call(
        "POST",
        f"/v1/governance/reviews/{review_id}/decision",
        body={"decision": "APPROVE", "reason": "agreed wording"},
        principal=REVIEWER,
        roles=STEWARD_ROLES,
        org_id=org_id,
    )
    report.record(
        "a different steward approves it",
        "PASS" if status == 200 else "FAIL",
        f"HTTP {status}" + ("" if status == 200 else f": {decided}"),
    )
    if status != 200:
        return

    new_head, new_keys = _published_head(api, org_id)
    expected = int(created.get("version", 0))
    report.record(
        "the version reads back as the published head",
        "PASS" if new_head == expected and new_head > base_version else "FAIL",
        f"published_version={new_head}, expected {expected}",
    )
    report.record(
        "every previously published concept survived",
        "PASS" if set(prior_keys) <= set(new_keys) else "FAIL",
        f"{len(new_keys)} concept(s) now published",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--org", default="sample-bank", help="organization slug")
    args = parser.parse_args()

    api = Api(args.base_url)
    report = Report(base_url=args.base_url)
    print("Ontology lifecycle")
    run(api, report, args.org)

    passed = sum(1 for c in report.checks if c.outcome == "PASS")
    print(f"\n{passed} passed, {len(report.failed)} failed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
