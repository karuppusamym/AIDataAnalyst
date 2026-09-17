"""R11-FP17 -- the alert rules say something true about metrics that exist.

`infra/monitoring/README.md` and `scripts/generate_prometheus_rule.py` both
named this file as the guard on the rules, and describe what it checks. It did
not exist: the four properties below were claimed, not tested, and the drift
that `generate_prometheus_rule.py`'s docstring calls "silent in the worst
possible way" had nothing watching it. Written 2026-09-17, to the description
those two files already gave.

What each check is for:

* **Every metric a rule reads is published somewhere.** An alert on a metric
  nobody emits never fires, and a rule that can never fire is worse than no
  rule -- it reads as coverage. This is a name check against the `Counter(...)`
  / `Gauge(...)` / `Histogram(...)` declarations under `src/`, not a check that
  anything is currently *scraping* them: 13 of the 19 series are published by
  the fleet scheduler and the graph projector, which expose no HTTP surface
  until `AIDA_WORKER_METRICS_PORT` is set, and that is a deployment decision
  this file has no business asserting about.
* **Every label matcher names a label the metric declares.**
  `aida_footprint_gaps{kind="..."}` is only an alert if `kind` is a real label
  with that value in it; `{kimd="..."}` parses, loads and never fires.
  Prometheus reports such a rule as healthy, so nothing else catches it.
* **Every alert declares `threshold_status`, from the closed set.** The README's
  whole account of which thresholds are measured and which are an operator's
  placeholder rests on that label being present and honest on every rule.
* **The generated PrometheusRule matches the source.** One file is the source of
  truth and the other is rendered from it; this fails when the rendered copy is
  stale, which is what the generator's docstring promises.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # `scripts/` is not an installed package
    sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_prometheus_rule import (  # noqa: E402
    OUTPUT_PATH,
    load_groups,
    render,
)

RULES_PATH = REPO_ROOT / "infra/monitoring/prometheus/rules/atlas.rules.yml"
SRC_ROOT = REPO_ROOT / "src"

#: `threshold_status` is the README's own closed set. `structural` means the
#: comparison needed no magnitude anybody chose; `placeholder` means the number
#: is a documented starting point an operator must replace.
THRESHOLD_STATUSES = frozenset({"structural", "placeholder"})

#: Series Prometheus itself synthesises, which no Atlas module declares.
PROMETHEUS_OWN_METRICS = frozenset({"up"})

#: Suffixes `prometheus_client` appends to a histogram's declared name. A rule
#: reads `aida_http_request_duration_seconds_bucket`; the code declares
#: `aida_http_request_duration_seconds`.
DERIVED_SUFFIXES = ("_bucket", "_count", "_sum", "_created", "_total")

_METRIC_REFERENCE = re.compile(r"\b(aida_[a-z0-9_]+|atlas:[a-z0-9_:]+|up)\b")
_LABEL_MATCHER = re.compile(
    r"\b(aida_[a-z0-9_]+|up)\s*\{([^}]*)\}",
)
_MATCHER_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"([^"]*)"')
_DECLARATION = re.compile(
    r"(?:Counter|Gauge|Histogram|Summary)\(\s*\n?\s*\"(aida_[a-z0-9_]+)\"(.*?)\n\)",
    re.DOTALL,
)
_LABELNAMES = re.compile(r"labelnames=\(([^)]*)\)")


def _rules() -> list[tuple[str, str, dict[str, Any]]]:
    """(group name, rule name, rule) for every rule in the source file."""
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    found: list[tuple[str, str, dict[str, Any]]] = []
    for group in document["groups"]:
        for rule in group["rules"]:
            found.append((group["name"], rule.get("alert") or rule["record"], rule))
    return found


def _declared_metrics() -> dict[str, frozenset[str]]:
    """Every `aida_*` series declared under `src/`, mapped to its label names.

    Parsed from the source rather than imported, deliberately: importing the
    four metrics modules would register them in this process's
    `prometheus_client` registry, and a test that mutates a process-global
    registry is a test that breaks whichever other test runs next.
    """
    declared: dict[str, frozenset[str]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "aida_" not in text:
            continue
        for name, body in _DECLARATION.findall(text):
            labels = _LABELNAMES.search(body)
            names = (
                frozenset(
                    part.strip().strip("\"'")
                    for part in labels.group(1).split(",")
                    if part.strip().strip("\"'")
                )
                if labels
                else frozenset()
            )
            declared[name] = names
    return declared


def _recording_rule_names() -> frozenset[str]:
    return frozenset(rule["record"] for _, _, rule in _rules() if "record" in rule)


def _resolve(name: str, declared: dict[str, frozenset[str]]) -> str | None:
    """The declared metric a rule's reference resolves to, or None."""
    if name in declared:
        return name
    for suffix in DERIVED_SUFFIXES:
        if name.endswith(suffix) and name[: -len(suffix)] in declared:
            return name[: -len(suffix)]
    return None


def test_the_rule_file_parses_and_declares_the_documented_number_of_rules() -> None:
    """24 rules: the README says "4 recording rules, 20 alerts"."""
    rules = _rules()
    alerts = [rule for _, _, rule in rules if "alert" in rule]
    records = [rule for _, _, rule in rules if "record" in rule]
    assert len(records) == 4, f"expected 4 recording rules, found {len(records)}"
    assert len(alerts) == 20, f"expected 20 alerts, found {len(alerts)}"


def test_every_metric_a_rule_reads_is_published_by_some_module() -> None:
    """A rule that names a metric nothing emits can never fire.

    It is also invisible: Prometheus loads it, reports it healthy, and evaluates
    it forever against an empty vector. Nothing but this check notices.
    """
    declared = _declared_metrics()
    assert declared, "no metric declarations were found under src/ -- the parser is broken"
    recorded = _recording_rule_names()
    unknown: list[str] = []
    for group, name, rule in _rules():
        for reference in set(_METRIC_REFERENCE.findall(rule["expr"])):
            if reference in PROMETHEUS_OWN_METRICS or reference in recorded:
                continue
            if _resolve(reference, declared) is None:
                unknown.append(f"{group}/{name} reads {reference}")
    assert unknown == [], "rules read metrics no module publishes: " + "; ".join(sorted(unknown))


def test_every_label_matcher_names_a_label_the_metric_declares() -> None:
    """`aida_footprint_gaps{kimd="..."}` loads, stays healthy, and never fires.

    Equality matchers on `__name__`-style meta labels and on `job`/`instance`
    (which Prometheus attaches at scrape time rather than the code declaring)
    are allowed; everything else has to be a label the declaration names.
    """
    declared = _declared_metrics()
    scrape_labels = {"job", "instance", "le", "atlas_process", "environment"}
    wrong: list[str] = []
    for group, name, rule in _rules():
        for metric, matchers in _LABEL_MATCHER.findall(rule["expr"]):
            if metric in PROMETHEUS_OWN_METRICS:
                continue
            resolved = _resolve(metric, declared)
            if resolved is None:
                continue  # reported by the test above
            for label, _operator, _value in _MATCHER_PAIR.findall(matchers):
                if label in scrape_labels or label in declared[resolved]:
                    continue
                wrong.append(f"{group}/{name}: {metric} has no label {label!r}")
    assert wrong == [], "rules match labels the metric does not declare: " + "; ".join(
        sorted(wrong)
    )


def test_every_alert_declares_a_threshold_status_from_the_closed_set() -> None:
    """The README's measured-versus-placeholder account rests on this label."""
    missing: list[str] = []
    for group, name, rule in _rules():
        if "alert" not in rule:
            continue
        status = (rule.get("labels") or {}).get("threshold_status")
        if status not in THRESHOLD_STATUSES:
            missing.append(f"{group}/{name} has threshold_status={status!r}")
    assert missing == [], "; ".join(sorted(missing))


def test_every_alert_declares_a_severity_and_a_runbook() -> None:
    """A page with no runbook is a page somebody has to reverse-engineer."""
    incomplete: list[str] = []
    for group, name, rule in _rules():
        if "alert" not in rule:
            continue
        labels = rule.get("labels") or {}
        annotations = rule.get("annotations") or {}
        if labels.get("severity") not in {"critical", "warning", "info"}:
            incomplete.append(f"{group}/{name}: severity={labels.get('severity')!r}")
        if not annotations.get("runbook"):
            incomplete.append(f"{group}/{name}: no runbook annotation")
        if not annotations.get("summary"):
            incomplete.append(f"{group}/{name}: no summary annotation")
    assert incomplete == [], "; ".join(sorted(incomplete))


def test_every_placeholder_alert_says_so_where_an_operator_will_see_it() -> None:
    """In the annotation, not only in a YAML comment.

    `generate_prometheus_rule.py`'s docstring makes exactly this promise: the
    comments do not survive the parse into the Kubernetes resource, so a
    placeholder that explains itself only in a comment explains itself to nobody
    who is paged by the generated copy.
    """
    silent: list[str] = []
    for group, name, rule in _rules():
        if (rule.get("labels") or {}).get("threshold_status") != "placeholder":
            continue
        described = " ".join(str(v) for v in (rule.get("annotations") or {}).values()).lower()
        if "placeholder" not in described:
            silent.append(f"{group}/{name}")
    assert silent == [], (
        "placeholder alerts whose annotations never say they are placeholders: "
        + "; ".join(sorted(silent))
    )


def test_the_generated_prometheus_rule_matches_the_source_file() -> None:
    """The drift the generator exists to prevent, actually checked.

    Imported rather than skipped-if-missing: a guard that disappears when its
    import breaks is the silent failure this whole file is here to stop.
    """
    expected = render(load_groups())
    actual = OUTPUT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is stale -- "
        "re-run `python scripts/generate_prometheus_rule.py`"
    )
    # Not vacuous: the comparison is sensitive to a one-character change.
    assert actual != expected.replace("aida-platform", "aida-platfrom", 1)
