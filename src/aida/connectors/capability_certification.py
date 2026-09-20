"""INV-9's enforcement clause: a connector's capability flags are derived from its certification.

**The rule.** A connector advertises only behaviour that is implemented and passing its
certification, and the flags say so *because a certification result says so* -- not because
someone typed `True`. This module is that derivation, plus the committed result it reads.

**What is where.**

* The *claim* is `<Connector>.DEFAULT_CAPABILITIES`, still hand-written. It is what the
  connector says about itself and is now an input to the derivation, never its output.
* The *certification result* is `capability_certification.json`, next to this module so the
  image ships it (the Dockerfile copies `src`, not `Docs`). One row per (connector, flag):
  status, the evidence tier the row stands on, what was probed, what came back, and the
  tests that produced it. `scripts/certify_connector_capabilities.py` is the only writer.
* The *derivation* is `derive_capabilities`: **a flag is advertised only if the connector
  claims it and its row is CERTIFIED** -- with one deliberate, visible exception below.
  The derived value can never exceed the claim, and a claim never certifies itself.

**Evidence tiers, and why they are never blurred.** `LIVE` means a probe ran against a real
running engine. `FIXTURE` means it ran against the connector's own driver double. A
fixture result is real evidence about the connector's logic and no evidence at all about
the engine, so `verify_result` refuses a row that cites a live probe under the fixture tier
or the reverse, and the tier travels with every flag to the capability endpoint.

**The one exception: `uncertified_claims`.** A flag a connector claims but whose probe did
not certify it is not silently lowered at the moment this landed -- lowering `explain`
makes the query gateway refuse to execute against that engine, which is an operator's
decision, not a side effect of a certification run. Such a flag is instead listed, with
what failed and the evidence, in the result's `uncertified_claims`, and *that list* is what
lets the derivation keep advertising it. It is explicit (a reader of the artifact sees every
one), it is closed (a claim that is neither certified nor listed derives to `False`, so a
newly claimed flag is not advertised until certified), and an adjacent test pins the exact
set so that changing it is a reviewed edit. The runner regenerates the list from the probe
results and never lowers anything itself: an entry leaves the list when its probe
certifies the flag, and a flag is lowered by lowering the claim in the connector's
`DEFAULT_CAPABILITIES`, which is a deliberate, reviewable edit -- not by deleting an entry,
which `verify_result` reports as a result that contradicts itself.

**Staleness.** Each connector's row set records a fingerprint of the code it was evaluated
against: the connector module plus every `aida.*` module it (transitively) imports, minus
this module, which is the judge and not the judged. `stale_connectors` names the files that
moved. It is checked by a test and by the script's `--check`, needs no database, and is what
stops a connector drifting from its certification silently.

Standard library plus `ConnectorCapabilities` only: nothing here opens a connection, runs a
statement or imports a driver, so importing it from a connector module cannot form a cycle
or widen INV-2's surface. The probes that execute SQL live in `tests/` and `scripts/`.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from aida.connectors.base import ConnectorCapabilities

CERTIFICATION_SCHEMA_VERSION = 1
CERTIFICATION_SUITE = "connector-capability-certification-v1"
RESULT_PATH = Path(__file__).with_name("capability_certification.json")

#: Every flag a connector can claim. Read from the dataclass, so a flag added there without
#: a probe row shows up in `verify_result` as an uncovered flag instead of being ignored.
CAPABILITY_FLAGS: tuple[str, ...] = tuple(f.name for f in dataclass_fields(ConnectorCapabilities))

STATUS_CERTIFIED = "CERTIFIED"
STATUS_NOT_CERTIFIED = "NOT_CERTIFIED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUSES = frozenset({STATUS_CERTIFIED, STATUS_NOT_CERTIFIED, STATUS_NOT_APPLICABLE})

TIER_LIVE = "LIVE"
TIER_FIXTURE = "FIXTURE"
TIERS = frozenset({TIER_LIVE, TIER_FIXTURE})

#: Why a row is NOT_CERTIFIED. A closed vocabulary so a reader can tell the three apart:
#: the probe ran and the behaviour was wrong; the probe ran and the behaviour is not
#: there; nothing here could run the probe at all.
REASON_PROBE_FAILED = "PROBE_FAILED"
REASON_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
REASON_NOT_EXERCISED = "NOT_EXERCISED"
REASON_CODES = frozenset({REASON_PROBE_FAILED, REASON_NOT_IMPLEMENTED, REASON_NOT_EXERCISED})

#: Modules a connector's fingerprint never includes: the derivation is the judge, not the
#: thing judged, and a change to it must not invalidate every engine's evidence.
_FINGERPRINT_EXCLUDED_MODULES = frozenset({"aida.connectors.capability_certification"})
_PROJECT_ROOTS = frozenset({"aida", "atlas"})


class CertificationResultError(ValueError):
    """The committed certification result is missing, unreadable or malformed."""


@dataclass(frozen=True, slots=True)
class CodeFingerprint:
    """The code a connector's rows were evaluated against.

    `files` maps each input's path (relative to `src/`, POSIX) to the SHA-256 of its
    LF-normalised bytes; `digest` is one hash over all of them. Both are kept so a stale
    report can name the file that moved instead of saying only that something did.
    """

    digest: str
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class FlagCertification:
    """One (connector, flag) cell of the certification result."""

    flag: str
    #: What the connector's own `DEFAULT_CAPABILITIES` said when this was evaluated.
    claimed: bool
    status: str
    #: The tier the probe ran at. `None` only when nothing could run it. Set on a
    #: NOT_CERTIFIED row too: "this failed live" and "this was never tried" differ.
    tier: str | None
    #: What was probed, in one sentence.
    probe: str
    #: What came back, in one sentence. Value-free: counts, names and costs, never rows.
    evidence: str
    #: The tests that produced the evidence, as pytest node ids.
    tests: tuple[str, ...] = ()
    #: Set exactly when `status` is NOT_CERTIFIED.
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class UncertifiedClaim:
    """A flag the connector claims, that no probe certified, and that is still advertised.

    Its presence is what holds the flag at its landing value (see the module docstring).
    """

    connector_type: str
    flag: str
    reason_code: str
    what_failed: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ConnectorCertification:
    connector_type: str
    fingerprint: CodeFingerprint
    #: Server and driver versions the LIVE rows ran against; empty for a fixture-only
    #: connector. Provenance for a reader, deliberately not part of the fingerprint: it
    #: would make the staleness gate depend on which machine ran it.
    environment: Mapping[str, str]
    claimed: Mapping[str, bool]
    flags: Mapping[str, FlagCertification]
    #: What the derivation produced when this was written. Stored so a diff of the
    #: artifact shows the effect of a re-certification; the runtime recomputes it and
    #: `verify_result` fails if the two disagree.
    derived: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class CertificationResult:
    schema_version: int
    suite: str
    #: The date the result was produced. A date, not a timestamp: the file is committed,
    #: and a timestamp would make every re-run a diff.
    certified_on: str
    connectors: Mapping[str, ConnectorCertification]
    uncertified_claims: tuple[UncertifiedClaim, ...] = field(default_factory=tuple)

    def held_flags(self, connector_type: str) -> frozenset[str]:
        return frozenset(
            claim.flag
            for claim in self.uncertified_claims
            if claim.connector_type == connector_type
        )


@dataclass(frozen=True, slots=True)
class FlagDerivation:
    """How one flag's advertised value came about. What the capability endpoint serves."""

    flag: str
    claimed: bool
    derived: bool
    #: The row's status, or `None` when the result has no row for it.
    status: str | None
    #: The tier of the evidence when the flag is CERTIFIED, else `None`.
    tier: str | None
    #: Advertised as `True` with no CERTIFIED row behind it, only because an explicit
    #: `uncertified_claims` entry says to keep it.
    held: bool


# --- reading and writing the result ------------------------------------------------------


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise CertificationResultError(message)


def _text(value: Any, where: str) -> str:
    _need(isinstance(value, str), f"{where} must be a string")
    return str(value)


def _flag_map(value: Any, where: str) -> dict[str, bool]:
    _need(isinstance(value, dict), f"{where} must be an object")
    out: dict[str, bool] = {}
    for key, item in value.items():
        _need(isinstance(item, bool), f"{where}.{key} must be a boolean")
        out[str(key)] = item
    return out


def _flag_certification(flag: str, data: Any, where: str) -> FlagCertification:
    _need(isinstance(data, dict), f"{where} must be an object")
    tier = data.get("tier")
    reason = data.get("reason_code")
    tests = data.get("tests", [])
    _need(isinstance(data.get("claimed"), bool), f"{where}.claimed must be a boolean")
    _need(isinstance(tests, list), f"{where}.tests must be a list")
    return FlagCertification(
        flag=flag,
        claimed=bool(data["claimed"]),
        status=_text(data.get("status"), f"{where}.status"),
        tier=None if tier is None else _text(tier, f"{where}.tier"),
        probe=_text(data.get("probe"), f"{where}.probe"),
        evidence=_text(data.get("evidence"), f"{where}.evidence"),
        tests=tuple(_text(test, f"{where}.tests[]") for test in tests),
        reason_code=None if reason is None else _text(reason, f"{where}.reason_code"),
    )


def result_from_data(data: Any) -> CertificationResult:
    """Build a `CertificationResult` from parsed JSON, rejecting a malformed shape."""
    _need(isinstance(data, dict), "the certification result must be a JSON object")
    _need(
        data.get("schema_version") == CERTIFICATION_SCHEMA_VERSION,
        f"unsupported certification schema_version {data.get('schema_version')!r}",
    )
    connectors: dict[str, ConnectorCertification] = {}
    raw_connectors = data.get("connectors")
    _need(isinstance(raw_connectors, dict), "`connectors` must be an object")
    for connector_type, raw in raw_connectors.items():
        where = f"connectors.{connector_type}"
        _need(isinstance(raw, dict), f"{where} must be an object")
        raw_fingerprint = raw.get("fingerprint")
        _need(isinstance(raw_fingerprint, dict), f"{where}.fingerprint must be an object")
        files = raw_fingerprint.get("files")
        _need(isinstance(files, dict), f"{where}.fingerprint.files must be an object")
        raw_flags = raw.get("flags")
        _need(isinstance(raw_flags, dict), f"{where}.flags must be an object")
        environment = raw.get("environment", {})
        _need(isinstance(environment, dict), f"{where}.environment must be an object")
        connectors[str(connector_type)] = ConnectorCertification(
            connector_type=str(connector_type),
            fingerprint=CodeFingerprint(
                digest=_text(raw_fingerprint.get("digest"), f"{where}.fingerprint.digest"),
                files={str(k): _text(v, f"{where}.fingerprint.files") for k, v in files.items()},
            ),
            environment={str(k): _text(v, f"{where}.environment") for k, v in environment.items()},
            claimed=_flag_map(raw.get("claimed"), f"{where}.claimed"),
            flags={
                str(flag): _flag_certification(str(flag), row, f"{where}.flags.{flag}")
                for flag, row in raw_flags.items()
            },
            derived=_flag_map(raw.get("derived"), f"{where}.derived"),
        )
    raw_claims = data.get("uncertified_claims", [])
    _need(isinstance(raw_claims, list), "`uncertified_claims` must be a list")
    claims: list[UncertifiedClaim] = []
    for index, raw_claim in enumerate(raw_claims):
        where = f"uncertified_claims[{index}]"
        _need(isinstance(raw_claim, dict), f"{where} must be an object")
        claims.append(
            UncertifiedClaim(
                connector_type=_text(raw_claim.get("connector_type"), f"{where}.connector_type"),
                flag=_text(raw_claim.get("flag"), f"{where}.flag"),
                reason_code=_text(raw_claim.get("reason_code"), f"{where}.reason_code"),
                what_failed=_text(raw_claim.get("what_failed"), f"{where}.what_failed"),
                evidence=_text(raw_claim.get("evidence"), f"{where}.evidence"),
            )
        )
    return CertificationResult(
        schema_version=CERTIFICATION_SCHEMA_VERSION,
        suite=_text(data.get("suite"), "suite"),
        certified_on=_text(data.get("certified_on"), "certified_on"),
        connectors=connectors,
        uncertified_claims=tuple(claims),
    )


def result_to_data(result: CertificationResult) -> dict[str, Any]:
    """The JSON-ready form of a result. Inverse of `result_from_data`."""
    return {
        "schema_version": result.schema_version,
        "suite": result.suite,
        "certified_on": result.certified_on,
        "flags": list(CAPABILITY_FLAGS),
        "connectors": {
            connector_type: {
                "fingerprint": {
                    "digest": cert.fingerprint.digest,
                    "files": dict(cert.fingerprint.files),
                },
                "environment": dict(cert.environment),
                "claimed": dict(cert.claimed),
                "derived": dict(cert.derived),
                "flags": {
                    flag: {
                        "claimed": row.claimed,
                        "status": row.status,
                        "tier": row.tier,
                        "reason_code": row.reason_code,
                        "probe": row.probe,
                        "evidence": row.evidence,
                        "tests": list(row.tests),
                    }
                    for flag, row in cert.flags.items()
                },
            }
            for connector_type, cert in result.connectors.items()
        },
        "uncertified_claims": [
            {
                "connector_type": claim.connector_type,
                "flag": claim.flag,
                "reason_code": claim.reason_code,
                "what_failed": claim.what_failed,
                "evidence": claim.evidence,
            }
            for claim in result.uncertified_claims
        ],
    }


def result_to_json(result: CertificationResult) -> str:
    """Deterministic text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(result_to_data(result), indent=2, sort_keys=True) + "\n"


@lru_cache(maxsize=4)
def _load(path: Path) -> CertificationResult:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CertificationResultError(
            f"the connector certification result {path.name} cannot be read: {exc}"
        ) from exc
    try:
        return result_from_data(json.loads(text))
    except json.JSONDecodeError as exc:
        raise CertificationResultError(f"{path.name} is not valid JSON: {exc}") from exc


def load_certification_result(path: Path | None = None) -> CertificationResult:
    """The committed certification result, cached per path.

    Raises `CertificationResultError` rather than returning an empty result: a platform
    whose certification cannot be read has no honest capability to advertise, and an empty
    result would silently lower `explain` everywhere and refuse every query for a reason
    nobody could see. It is a packaging error and it fails loudly, at import.
    """
    return _load(RESULT_PATH if path is None else path.resolve())


def reset_certification_cache() -> None:
    """Forget the cached result. For tests that swap the file or the loader."""
    _load.cache_clear()


# --- derivation --------------------------------------------------------------------------


def derive_flags(
    connector_type: str,
    claimed: ConnectorCapabilities,
    result: CertificationResult | None = None,
) -> tuple[FlagDerivation, ...]:
    """Each flag's advertised value, with the reason it came out that way.

    `advertised = claimed and (CERTIFIED or explicitly held)`. `advertised <= claimed` is
    structural, not tested-for: the only `True` comes out of an `and` with the claim, so no
    result -- however it is edited -- can make a connector advertise what it does not claim.
    """
    loaded = result if result is not None else load_certification_result()
    certification = loaded.connectors.get(connector_type)
    held = loaded.held_flags(connector_type)
    derivations: list[FlagDerivation] = []
    for flag in CAPABILITY_FLAGS:
        claim = bool(getattr(claimed, flag))
        row = certification.flags.get(flag) if certification is not None else None
        certified = row is not None and row.status == STATUS_CERTIFIED
        holds = claim and not certified and flag in held
        derivations.append(
            FlagDerivation(
                flag=flag,
                claimed=claim,
                derived=claim and (certified or holds),
                status=None if row is None else row.status,
                tier=row.tier if certified and row is not None else None,
                held=holds,
            )
        )
    return tuple(derivations)


def derive_capabilities(
    connector_type: str,
    claimed: ConnectorCapabilities,
    result: CertificationResult | None = None,
) -> ConnectorCapabilities:
    """`claimed`, narrowed to what the certification result supports.

    This is the value the query gateway, the capability endpoint and every discovery
    surface read. A connector whose module is absent from the result advertises nothing
    beyond the `catalogs`/`schemas` defaults' claim of `False` -- it is uncertified.
    """
    return ConnectorCapabilities(
        **{item.flag: item.derived for item in derive_flags(connector_type, claimed, result)}
    )


# --- the code fingerprint ----------------------------------------------------------------


def default_source_root() -> Path:
    """`src/`, where this module's package tree lives."""
    return Path(__file__).resolve().parents[2]


def _module_file(source_root: Path, module: str) -> Path | None:
    parts = module.split(".")
    as_module = source_root.joinpath(*parts).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = source_root.joinpath(*parts, "__init__.py")
    return as_package if as_package.is_file() else None


def _project_imports(path: Path) -> set[str]:
    """Dotted names of every project module `path` imports, at any nesting depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            # `from aida.connectors import base` names a module, not an attribute.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".")[0] in _PROJECT_ROOTS}


def fingerprint_inputs(connector_type: str, source_root: Path | None = None) -> tuple[Path, ...]:
    """The connector module and every project module it transitively imports."""
    root = source_root if source_root is not None else default_source_root()
    entry = _module_file(root, f"aida.connectors.{connector_type}")
    if entry is None:
        raise CertificationResultError(f"no connector module for {connector_type!r}")
    seen: dict[Path, None] = {entry: None}
    pending = [entry]
    while pending:
        current = pending.pop()
        for module in sorted(_project_imports(current)):
            if module in _FINGERPRINT_EXCLUDED_MODULES:
                continue
            target = _module_file(root, module)
            if target is not None and target not in seen:
                seen[target] = None
                pending.append(target)
    return tuple(sorted(seen))


def _file_digest(path: Path) -> str:
    # LF-normalised, as `aida.source_identity` does: a Windows checkout's line endings
    # must not make the same commit read as different code.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def compute_fingerprint(connector_type: str, source_root: Path | None = None) -> CodeFingerprint:
    root = source_root if source_root is not None else default_source_root()
    files = {
        path.relative_to(root).as_posix(): _file_digest(path)
        for path in fingerprint_inputs(connector_type, root)
    }
    combined = hashlib.sha256(
        "".join(f"{name}:{digest}\n" for name, digest in sorted(files.items())).encode("utf-8")
    ).hexdigest()
    return CodeFingerprint(digest=combined, files=files)


@dataclass(frozen=True, slots=True)
class Staleness:
    """One connector whose code is no longer what its certification was evaluated against."""

    connector_type: str
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]

    def describe(self) -> str:
        parts = [
            f"{label} {', '.join(names)}"
            for label, names in (
                ("changed:", self.changed),
                ("newly imported:", self.added),
                ("no longer imported:", self.removed),
            )
            if names
        ]
        detail = "; ".join(parts) or "the combined digest differs"
        return f"{self.connector_type} ({detail})"


def stale_connectors(
    result: CertificationResult | None = None,
    *,
    source_root: Path | None = None,
    connector_types: Iterable[str] | None = None,
) -> tuple[Staleness, ...]:
    """Connectors whose current code fingerprint differs from the one certified.

    Needs no database: LIVE evidence is read from the committed result and only ever
    re-produced by running the script. A connector missing from the result entirely is
    reported stale too (every input "newly imported"), because it has no certification.
    """
    loaded = result if result is not None else load_certification_result()
    wanted = sorted(connector_types) if connector_types is not None else sorted(loaded.connectors)
    stale: list[Staleness] = []
    for connector_type in wanted:
        current = compute_fingerprint(connector_type, source_root)
        certification = loaded.connectors.get(connector_type)
        certified_files: Mapping[str, str] = (
            certification.fingerprint.files if certification is not None else {}
        )
        if certification is not None and certification.fingerprint.digest == current.digest:
            continue
        stale.append(
            Staleness(
                connector_type=connector_type,
                changed=tuple(
                    sorted(
                        name
                        for name, digest in current.files.items()
                        if name in certified_files and certified_files[name] != digest
                    )
                ),
                added=tuple(sorted(set(current.files) - set(certified_files))),
                removed=tuple(sorted(set(certified_files) - set(current.files))),
            )
        )
    return tuple(stale)


# --- consistency -------------------------------------------------------------------------


def verify_result(
    result: CertificationResult,
    *,
    live_probe_modules: Sequence[str],
    claims: Mapping[str, ConnectorCapabilities] | None = None,
) -> list[str]:
    """Every way `result` contradicts itself or the claims it was made against.

    Structural only, and database-free, so it runs in CI. `live_probe_modules` names the
    test files that may produce LIVE evidence (path prefixes): a row that cites one under
    the FIXTURE tier, or cites anything else under LIVE, is the blurring this module exists
    to prevent. `claims`, when given, are the connectors' *current* declarations; a result
    written against a different claim is reported even if the code fingerprint happens to
    match.
    """
    problems: list[str] = []

    def is_live_test(node_id: str) -> bool:
        return any(
            node_id == module or node_id.startswith(f"{module}::") for module in live_probe_modules
        )

    for connector_type, cert in sorted(result.connectors.items()):
        missing = [flag for flag in CAPABILITY_FLAGS if flag not in cert.flags]
        if missing:
            problems.append(f"{connector_type}: no probe row for {missing}")
        extra = sorted(set(cert.flags) - set(CAPABILITY_FLAGS))
        if extra:
            problems.append(f"{connector_type}: rows for unknown flags {extra}")
        for flag, row in sorted(cert.flags.items()):
            where = f"{connector_type}.{flag}"
            if row.status not in STATUSES:
                problems.append(f"{where}: unknown status {row.status!r}")
                continue
            if row.tier is not None and row.tier not in TIERS:
                problems.append(f"{where}: unknown tier {row.tier!r}")
            if cert.claimed.get(flag) != row.claimed:
                problems.append(f"{where}: row.claimed disagrees with the connector's claimed map")
            if row.status == STATUS_CERTIFIED:
                if row.tier is None or not row.tests:
                    problems.append(f"{where}: CERTIFIED needs an evidence tier and its tests")
                if row.reason_code is not None:
                    problems.append(f"{where}: a CERTIFIED row carries a reason_code")
            elif row.status == STATUS_NOT_CERTIFIED:
                if row.reason_code not in REASON_CODES:
                    problems.append(
                        f"{where}: NOT_CERTIFIED needs a reason_code from {sorted(REASON_CODES)}"
                    )
            elif row.status == STATUS_NOT_APPLICABLE:
                if row.claimed:
                    problems.append(f"{where}: NOT_APPLICABLE but the connector claims it")
                if row.tier is not None or row.tests:
                    problems.append(f"{where}: NOT_APPLICABLE carries an evidence tier or tests")
            if row.tier == TIER_LIVE:
                foreign = [t for t in row.tests if not is_live_test(t)]
                if foreign or not row.tests:
                    problems.append(f"{where}: labelled LIVE but cites non-live tests {foreign}")
            if row.tier == TIER_FIXTURE:
                live = [t for t in row.tests if is_live_test(t)]
                if live:
                    problems.append(f"{where}: labelled FIXTURE but cites live probes {live}")
        expected_derived = {
            item.flag: item.derived
            for item in derive_flags(
                connector_type,
                ConnectorCapabilities(**{f: cert.claimed.get(f, False) for f in CAPABILITY_FLAGS}),
                result,
            )
        }
        if dict(cert.derived) != expected_derived:
            problems.append(f"{connector_type}: stored `derived` differs from the derivation")
        for flag in CAPABILITY_FLAGS:
            if cert.derived.get(flag) and not cert.claimed.get(flag):
                problems.append(f"{connector_type}.{flag}: advertised without being claimed")
        if claims is not None and connector_type in claims:
            live_claim = claims[connector_type]
            drifted = [
                flag
                for flag in CAPABILITY_FLAGS
                if bool(getattr(live_claim, flag)) != cert.claimed.get(flag)
            ]
            if drifted:
                problems.append(
                    f"{connector_type}: DEFAULT_CAPABILITIES changed since certification: {drifted}"
                )

    expected_claims = {
        (connector_type, flag)
        for connector_type, cert in result.connectors.items()
        for flag, row in cert.flags.items()
        if row.claimed and row.status != STATUS_CERTIFIED
    }
    listed = {(claim.connector_type, claim.flag) for claim in result.uncertified_claims}
    for connector_type, flag in sorted(expected_claims - listed):
        problems.append(
            f"{connector_type}.{flag}: claimed but not certified, and not listed in "
            "uncertified_claims"
        )
    for connector_type, flag in sorted(listed - expected_claims):
        problems.append(
            f"{connector_type}.{flag}: listed in uncertified_claims but is not a claimed, "
            "uncertified flag"
        )
    for claim in result.uncertified_claims:
        if claim.reason_code not in REASON_CODES:
            problems.append(
                f"{claim.connector_type}.{claim.flag}: bad reason_code {claim.reason_code!r}"
            )
    return problems


# --- the human-readable page -------------------------------------------------------------


def render_markdown(result: CertificationResult) -> str:
    """The published page for `result`. Byte-stable: no clock, no environment."""
    lines: list[str] = [
        "# Connector capability certification",
        "",
        "INV-9: a connector advertises only behaviour that is implemented and passing its",
        "certification. This page renders the committed result",
        "(`src/aida/connectors/capability_certification.json`), which",
        "`scripts/certify_connector_capabilities.py` writes and `--check` verifies. Every",
        "capability flag the platform advertises is **derived** from it: a flag is advertised",
        "only if the connector claims it (`DEFAULT_CAPABILITIES`) and its row here is CERTIFIED.",
        "",
        "**Evidence tiers.** LIVE = probed against a real running engine. FIXTURE = proven against",
        "the connector's own driver double; it says the connector's logic works and says nothing",
        "about a real engine. A fixture result is never labelled live.",
        "",
        f"Suite `{result.suite}`, produced {result.certified_on}. Regenerating a connector's LIVE",
        "rows needs the sample containers running; `--check` needs nothing.",
        "",
        "## Summary",
        "",
        "| Connector | Advertised | LIVE | FIXTURE | Not certified | Not applicable | Held |",
        "|---|---|---|---|---|---|---|",
    ]
    for connector_type, cert in sorted(result.connectors.items()):
        rows = list(cert.flags.values())
        live = sum(1 for r in rows if r.status == STATUS_CERTIFIED and r.tier == TIER_LIVE)
        fixture = sum(1 for r in rows if r.status == STATUS_CERTIFIED and r.tier == TIER_FIXTURE)
        not_certified = sum(1 for r in rows if r.status == STATUS_NOT_CERTIFIED)
        not_applicable = sum(1 for r in rows if r.status == STATUS_NOT_APPLICABLE)
        advertised_count = sum(1 for value in cert.derived.values() if value)
        held_count = len(result.held_flags(connector_type))
        lines.append(
            f"| {connector_type} | {advertised_count} | {live} | {fixture} | {not_certified} "
            f"| {not_applicable} | {held_count} |"
        )

    lines += ["", "## Uncertified claims", ""]
    if result.uncertified_claims:
        lines += [
            "These flags are claimed by the connector, were **not** certified by their probe,",
            "and are still advertised because this list says to keep them. Lowering one is a",
            "decision about query execution (`explain` gates the gateway), so a certification run",
            "never does it: lower the claim in the connector's `DEFAULT_CAPABILITIES` instead.",
            "",
            "| Connector | Flag | Reason | What failed | Evidence |",
            "|---|---|---|---|---|",
        ]
        for claim in result.uncertified_claims:
            lines.append(
                f"| {claim.connector_type} | `{claim.flag}` | {claim.reason_code} "
                f"| {_cell(claim.what_failed)} | {_cell(claim.evidence)} |"
            )
    else:
        lines.append("None: every claimed flag is certified.")

    unclaimed = [
        (connector_type, flag, row)
        for connector_type, cert in sorted(result.connectors.items())
        for flag, row in cert.flags.items()
        if not row.claimed and row.status == STATUS_CERTIFIED
    ]
    lines += ["", "## Certified but not claimed", ""]
    if unclaimed:
        lines += [
            "The probe passes and the connector declares the flag `False`, so it is not",
            "advertised. Nothing raises a claim automatically: that is a decision for the",
            "connector owner, and a fixture-tier pass says nothing about a real engine.",
            "",
            "| Connector | Flag | Tier | Evidence |",
            "|---|---|---|---|",
        ]
        for connector_type, flag, unclaimed_row in unclaimed:
            lines.append(
                f"| {connector_type} | `{flag}` | {unclaimed_row.tier} "
                f"| {_cell(unclaimed_row.evidence)} |"
            )
    else:
        lines.append("None.")

    for connector_type, cert in sorted(result.connectors.items()):
        lines += ["", f"## {connector_type}", ""]
        env = ", ".join(f"{key} {value}" for key, value in sorted(cert.environment.items()))
        lines.append(f"Code fingerprint `{cert.fingerprint.digest[:16]}` over:")
        lines.append("")
        lines.extend(f"- `{name}`" for name in sorted(cert.fingerprint.files))
        if env:
            lines += ["", f"LIVE rows ran against: {env}."]
        lines += [
            "",
            "| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |",
            "|---|---|---|---|---|---|---|",
        ]
        held = result.held_flags(connector_type)
        for flag in CAPABILITY_FLAGS:
            row = cert.flags.get(flag)
            if row is None:
                lines.append(f"| `{flag}` | | | MISSING | | | |")
                continue
            status = row.status + (f" ({row.reason_code})" if row.reason_code else "")
            advertised = "yes" if cert.derived.get(flag) else "no"
            if flag in held:
                advertised += " (held)"
            lines.append(
                f"| `{flag}` | {'yes' if row.claimed else 'no'} | {advertised} | {status} "
                f"| {row.tier or '-'} | {_cell(row.probe)} | {_cell(row.evidence)} |"
            )
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell: no bare pipe, no newline."""
    return text.replace("|", "\\|").replace("\n", " ")
