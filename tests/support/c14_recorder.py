"""A pytest plugin that writes each test's outcome and recorded properties to a JSON file.

Used only by `scripts/certify_connector_capabilities.py`, which loads it into a *child* pytest
process (`-p tests.support.c14_recorder`) and names the output file in `C14_RESULTS`. A child
process rather than an in-process `pytest.main` because the certification must run with
`AIDA_ENVIRONMENT` absent, exactly as CI runs the suite, and because a probe run must not share
interpreter state (settings caches, patched modules) with the script that reads its results.

Nothing here decides what an outcome *means*: it only carries `passed` / `failed` / `skipped`,
the `record_property` pairs a probe recorded, and a one-line failure or skip reason. The runner
turns those into certification rows.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

_RESULTS: dict[str, dict[str, Any]] = {}


def _one_line(text: str) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[-1][:400] if lines else ""


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    record = _RESULTS.setdefault(
        report.nodeid, {"outcome": "passed", "properties": {}, "detail": ""}
    )
    if report.when == "call":
        record["properties"].update({str(k): str(v) for k, v in report.user_properties})
    if report.outcome == "failed":
        record["outcome"] = "failed"
        record["detail"] = _one_line(report.longreprtext)
    elif report.outcome == "skipped" and record["outcome"] != "failed":
        record["outcome"] = "skipped"
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
        record["detail"] = _one_line(str(reason))


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = os.environ.get("C14_RESULTS")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_RESULTS, handle, indent=2, sort_keys=True)
