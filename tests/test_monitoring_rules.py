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
    """27 rules: the README says "4 recording rules, 23 alerts" (20 from R11-FP17, and the
    leader-election and drafter-consumer alerts of R11-AUD04 and R11-AUD03)."""
    rules = _rules()
    alerts = [rule for _, _, rule in rules if "alert" in rule]
    records = [rule for _, _, rule in rules if "record" in rule]
    assert len(records) == 4, f"expected 4 recording rules, found {len(records)}"
    assert len(alerts) == 23, f"expected 23 alerts, found {len(alerts)}"


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


# --- R11-AUD03 / R11-AUD04: the leader-election and drafter-consumer alerts -------------------
#
# The checks above say every alert reads a metric something publishes. These say the three added
# for the two operability signals are the alerts their comments claim: the windows still follow
# from the numbers they were derived from, the drafter rule still cannot fire where its gauge is
# not published, and every process that opens a listener is actually scraped.

COMPOSE_PATH = REPO_ROOT / "compose.yaml"
PROMETHEUS_CONFIG_PATH = REPO_ROOT / "infra/monitoring/prometheus/prometheus.yml"
POD_MONITOR_PATH = REPO_ROOT / "infra/monitoring/k8s/podmonitor.yaml"
RUNBOOK_PATH = REPO_ROOT / "Docs/40-engineering/07-local-runbook.md"
LEADERSHIP_MODULE_PATH = SRC_ROOT / "aida/scheduler_leadership.py"

_DURATION = re.compile(r"^(\d+)([smh])$")
_LISTENER_CALL = re.compile(r'serve_worker_metrics\(\s*settings,\s*process="([a-z-]+)"')


def _alert(name: str) -> dict[str, Any]:
    for _group, alert_name, rule in _rules():
        if alert_name == name:
            return rule
    raise AssertionError(f"no alert named {name!r} in {RULES_PATH.name}")


def _duration_seconds(text: str) -> int:
    match = _DURATION.match(text)
    assert match, f"a duration this test cannot read: {text!r}"
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def test_the_no_leader_window_outlasts_the_failover_the_runbook_recommends() -> None:
    """The `for` is derived, not measured -- the rule's own comment says so -- and the
    derivation has inputs that live elsewhere: the keepalive settings the runbook tells an
    operator to set, the standby's retry, and the scrape and evaluation intervals. Change one of
    them without this rule and either a failover that works pages, or one that does not is
    waited out by an alert that fires too early to mean anything."""
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    keepalives: dict[str, int] = {}
    for name in ("idle", "interval", "count"):
        found = re.search(rf"tcp_keepalives_{name}\s*=\s*(\d+)", runbook)
        assert found, f"the runbook no longer gives a value for tcp_keepalives_{name}"
        keepalives[name] = int(found.group(1))
    # PostgreSQL drops the session after `idle` seconds of silence and `count` unanswered
    # probes, `interval` apart.
    detection = keepalives["idle"] + keepalives["interval"] * keepalives["count"]
    retry = re.search(
        r"^STANDBY_RETRY_SECONDS\s*=\s*([0-9.]+)",
        LEADERSHIP_MODULE_PATH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert retry, "STANDBY_RETRY_SECONDS is no longer a plain constant this test can read"
    prometheus = yaml.safe_load(PROMETHEUS_CONFIG_PATH.read_text(encoding="utf-8"))["global"]
    scrape_and_evaluation = _duration_seconds(prometheus["scrape_interval"]) + _duration_seconds(
        prometheus["evaluation_interval"]
    )

    window = _duration_seconds(_alert("AtlasSchedulerNoLeader")["for"])

    needed = detection + float(retry.group(1)) + scrape_and_evaluation
    assert window > needed, (
        f"AtlasSchedulerNoLeader waits {window}s but a failover with the runbook's keepalives "
        f"takes {needed}s ({detection}s detection + retry + one scrape and one evaluation)"
    )


def _runbook_section() -> str:
    """Section 9c of the local runbook: the text an operator reads about these signals."""
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    start = runbook.index("## 9c.")
    return runbook[start : runbook.index("\n## 10.", start)]


def test_every_series_the_runbook_names_is_one_the_code_declares() -> None:
    """Section 9c tells an operator which series to read and what their labels mean. A series
    renamed in the code and not in the runbook is a query that quietly returns nothing."""
    declared = _declared_metrics()
    named = set(re.findall(r"\baida_[a-z0-9_]+", _runbook_section()))
    assert len(named) >= 4, f"section 9c names {sorted(named)}; is the test reading the right text?"

    unknown = sorted(name for name in named if _resolve(name, declared) is None)
    assert unknown == [], f"the runbook names series no module publishes: {unknown}"
    # The labels the runbook documents are the labels the code declares.
    assert "transition" in declared["aida_scheduler_leadership_transitions_total"]
    assert "consumer_group" in declared["aida_newly_created_table_drafter_consumer_up"]


_ALERT_NAME = re.compile(r"\bAtlas[A-Z][A-Za-z]+\b")


def test_every_alert_the_documents_name_exists() -> None:
    """The runbook, the monitoring README and the workers note name alerts by hand, and the rule
    file names them in each other's annotations. One renamed in the rule file and not there is a
    page that points an operator at nothing."""
    alerts = {name for _group, name, rule in _rules() if "alert" in rule}
    documents = {
        "runbook section 9c": _runbook_section(),
        "monitoring README": (REPO_ROOT / "infra/monitoring/README.md").read_text(encoding="utf-8"),
        "workers and workflows": (
            REPO_ROOT / "Docs/10-architecture/08-workers-and-workflows.md"
        ).read_text(encoding="utf-8"),
        "the rule file": RULES_PATH.read_text(encoding="utf-8"),
    }
    missing = [
        f"{where} names {name}"
        for where, text in documents.items()
        for name in sorted(set(_ALERT_NAME.findall(text)))
        if name not in alerts
    ]
    assert missing == [], "; ".join(missing)


def test_the_summaries_state_the_window_the_rule_waits() -> None:
    """The page says "for five minutes"; the rule must wait five. The two live in one file and
    are edited separately."""
    words = {5: "five", 15: "fifteen"}
    for name in ("AtlasSchedulerNoLeader", "AtlasNewlyCreatedTableDrafterConsumerDown"):
        rule = _alert(name)
        minutes = _duration_seconds(rule["for"]) // 60
        assert minutes in words, f"{name} waits {rule['for']}: add it to this test's words"
        assert f"{words[minutes]} minutes" in rule["annotations"]["summary"], name


def test_the_no_leader_alert_speaks_only_for_replicas_that_report() -> None:
    """`or absent(...)` would make it claim "no leader" about a scheduler that simply never
    opened its metrics port, which `AtlasTargetDown` already reports as what it is."""
    expr = _alert("AtlasSchedulerNoLeader")["expr"]
    assert "aida_scheduler_is_leader" in expr
    assert "absent" not in expr and "vector(" not in expr


def test_the_flapping_alert_counts_only_lost_leadership() -> None:
    """Every deploy and every restart acquires leadership. Only a loss is trouble."""
    expr = _alert("AtlasSchedulerLeadershipFlapping")["expr"]
    assert 'transition="lost"' in expr
    assert "acquired" not in expr


def test_the_drafter_alert_can_only_fire_where_the_worker_publishes_the_gauge() -> None:
    """A missing series means the feature is off or the port is unset, and neither is an outage.
    So the rule reads the gauge and nothing that would turn its absence into a value."""
    rule = _alert("AtlasNewlyCreatedTableDrafterConsumerDown")
    expr = " ".join(rule["expr"].split())
    assert expr == "aida_newly_created_table_drafter_consumer_up == 0", expr


def test_the_drafter_alert_says_a_missing_broker_is_expected_on_the_default_stack() -> None:
    """Where an operator reads it: the annotation, not a YAML comment the Kubernetes copy loses.
    `auto_enqueue_on_ingest` defaults to true and the default stack has no broker."""
    description = " ".join(
        _alert("AtlasNewlyCreatedTableDrafterConsumerDown")["annotations"]["description"].split()
    ).lower()
    for phrase in ("expected on the default stack", "no broker", "auto_enqueue_on_ingest"):
        assert phrase in description, f"the drafter alert no longer says {phrase!r}"


def test_every_process_that_opens_a_metrics_listener_is_scraped_and_gets_the_port() -> None:
    """The rule `prometheus.yml` used to state -- a job is added at the same time as the first
    series its process publishes, not before -- as a check instead of a sentence.

    Three things make a listener reachable: a scrape job (or PodMonitor) that names the process,
    and, in the compose stack, the port variable reaching that service. Miss any of them and the
    series are published into a process nothing can read, which is the R11-FP17 finding again."""
    processes: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        processes.update(_LISTENER_CALL.findall(path.read_text(encoding="utf-8")))
    assert {"fleet-scheduler", "graph-projector", "metadata-worker"} <= processes, processes

    jobs = {
        job["job_name"]: job
        for job in yaml.safe_load(PROMETHEUS_CONFIG_PATH.read_text(encoding="utf-8"))[
            "scrape_configs"
        ]
    }
    monitors = {
        document["metadata"]["name"]
        for document in yaml.safe_load_all(POD_MONITOR_PATH.read_text(encoding="utf-8"))
        if document
    }
    services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]

    problems: list[str] = []
    for process in sorted(processes):
        job = jobs.get(f"atlas-{process}")
        if job is None:
            problems.append(f"{process}: no scrape job atlas-{process}")
        elif job["static_configs"][0]["targets"] != [f"{process}:9108"]:
            problems.append(f"{process}: job atlas-{process} does not scrape {process}:9108")
        if f"aida-{process}" not in monitors:
            problems.append(f"{process}: no PodMonitor aida-{process}")
        environment = (services.get(process) or {}).get("environment") or {}
        if "AIDA_WORKER_METRICS_PORT" not in environment:
            problems.append(f"{process}: compose does not pass AIDA_WORKER_METRICS_PORT")
    assert problems == [], "; ".join(problems)
