#!/usr/bin/env python3
"""Generate the external destination and credential inventory.

Review 2026-09-05, section 6 item 7: "an inventory of every external destination
and credential reference, distinguishing configured, approved, active, healthy
and verified." The tracker records that item as ◐ -- the archive, SIEM and
notification destinations each grew an explicit configured/unavailable/delivered
state during the first pass, but nothing collected them into one place, so there
was still no answer to "what can this deployment talk to, and which of those
claims has anyone actually checked".

Built on `scripts/generate_surface_control_matrix.py`'s model, and for the same
reasons:

**Everything is derived, nothing is asserted.** Rows come from
`atlas.platform.config.Settings.model_fields` -- every field whose *name* denotes
a host, URL, endpoint, DSN, bucket, key, token or credential reference -- and the
consumer column comes from searching `src/` for real attribute reads of that
field. There is no hand-maintained destination list here, because a
hand-maintained one is wrong the first time somebody adds a webhook.

**A cell the analysis cannot determine says `unknown`.** The gap list is the
useful output. An inventory whose every cell is filled in by a guessing analyser
is worse than one that says where it could not see.

**No secret value is ever emitted.** This file writes a document into the
repository. Several of the settings below are credentials -- `openai_api_key`,
`secrets_vault_token`, `neo4j_password`, `audit_hmac_key`, `object_store_secret_key`
-- and `database_url`'s own shipped default embeds a password in its userinfo. So
this generator never prints a setting's value: not for credentials, not for
destinations, not even the defaults that are already visible in `config.py`. It
prints the setting *name*, a *characterization* of the default (`unset`, `empty`,
`placeholder`, `localhost`, ...) and, for destinations only, the *host and scheme*
with any userinfo stripped. A generated document that leaked a token would be a
far worse defect than the missing inventory it was written to fix.

The five states, which are NOT synonyms
---------------------------------------
The same point `Docs/90-reference/` 's capability register makes with its four
columns. Conflating these is how "we have an archive" comes to mean five
different things to five people:

* **Configured** -- a value naming a real destination is present. Derived here
  from the shipped default only: this generator reads source, not a deployment's
  environment, so it answers "does the software ship pointing anywhere" and says
  so.
* **Approved** -- the use of that destination passes a governance check before
  traffic flows (an APPROVED route row, an entitlement, a policy decision), as
  opposed to being reachable by whoever set the variable.
* **Active** -- a feature flag actually lets the code open the connection. Almost
  everything outbound in this repository is off by default, deliberately, and
  that is a *different* fact from being unconfigured.
* **Healthy** -- a probe reports on it. `/health/ready` covers two destinations;
  the rest have no probe, and that is a finding, not an omission from this table.
* **Verified** -- a real destination has acknowledged real traffic. This is
  runtime evidence, and no static generator can produce it. Every row therefore
  says `unknown`, which is the same answer F01/F04/§6 item 8 give: filesystem
  archival and loopback delivery are proven, a real bucket and a real SOC
  collector are not.

Usage
-----
    python scripts/generate_destination_inventory.py           # write the doc
    python scripts/generate_destination_inventory.py --check   # fail if stale
    python scripts/generate_destination_inventory.py --stdout  # print only
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from atlas.platform.config import Settings  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "Docs" / "50-security" / "destination-and-credential-inventory.md"
CONFIG_MODULE = SRC_ROOT / "atlas" / "platform" / "config.py"
READINESS_MODULE = SRC_ROOT / "aida" / "readiness.py"

UNKNOWN = "unknown"

# --- Which fields are in scope ---------------------------------------------
#
# Matched on the field NAME, so a destination added later is picked up without
# this file changing. The two lists are the review's own words ("host, URL,
# endpoint, DSN, bucket, key or token") split by what the row means: a place
# traffic goes, versus a secret or a reference used to reach one.
DESTINATION_TOKENS = (
    "url",
    "urls",
    "uri",
    "endpoint",
    "endpoints",
    "address",
    "addresses",
    "servers",
    "dsn",
    "bucket",
    "root",
    "host",
    "issuer",
)
CREDENTIAL_TOKENS = (
    "key",
    "keys",
    "token",
    "password",
    "secret",
    "reference",
    "jwks",
    "user",
    "username",
)
# Only string-shaped fields can name a destination or hold a credential. This
# excludes the timeouts, batch sizes and boolean switches whose names happen to
# contain a matching token (`dq_itsm_webhook_timeout_seconds`,
# `delivery_webhook_verify_tls`, `oidc_jwks_cache_seconds`).
STRING_ANNOTATIONS = ("str", "SecretStr", "dict[str, str]")

# Defaults that name nothing reachable. `internal://` is this codebase's own
# deliberate placeholder scheme -- `aida.siem_routing.parse_siem_endpoint`
# resolves it to NOT_CONFIGURED -- and the others are the equivalent idioms.
PLACEHOLDER_VALUES = frozenset({"", "unset", "development-only-change-me", "none"})
PLACEHOLDER_SCHEMES = frozenset({"internal"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})

# Names that mean "a governance decision gates this use". Same idea as the
# surface matrix's call-name sets: the inventory and the code agree on what
# "approved" means because both point at these identifiers.
APPROVAL_MARKERS = frozenset(
    {
        "APPROVED",
        "ApprovalStatus",
        "GovernanceReview",
        "evaluate_entitlement",
        "authorize_enforced",
        "require_approved_route",
    }
)


@dataclass(frozen=True, slots=True)
class Row:
    setting: str
    kind: str
    points_at: str
    default: str
    inert: str
    consumed_by: str
    configured: str
    approved: str
    active: str
    healthy: str
    verified: str

    @property
    def unknown_cells(self) -> list[str]:
        return [
            name
            for name, value in (
                ("points at", self.points_at),
                ("default", self.default),
                ("inert", self.inert),
                ("consumer", self.consumed_by),
                ("configured", self.configured),
                ("approved", self.approved),
                ("active", self.active),
                ("healthy", self.healthy),
                ("verified", self.verified),
            )
            if value == UNKNOWN
        ]


def _annotation_text(annotation: object) -> str:
    return str(annotation).replace("typing.", "").replace("pydantic.types.", "")


def _tokens(name: str) -> list[str]:
    return name.split("_")


def classify(name: str, annotation: object) -> str | None:
    """`destination`, `credential`, or None when the field is neither.

    Destination wins when a name carries both kinds of token, because that is
    what the field addresses: `oidc_jwks_url` is a URL this process fetches from,
    not a secret it holds, even though `jwks` is key material.
    """
    text = _annotation_text(annotation)
    if not any(shape in text for shape in STRING_ANNOTATIONS):
        return None
    if "bool" in text or "int" in text or "float" in text:
        return None
    tokens = _tokens(name)
    if any(token in DESTINATION_TOKENS for token in tokens):
        return "destination"
    if any(token in CREDENTIAL_TOKENS for token in tokens):
        # `hmac_signing_vault_key_name` names a key, it is not one; treated as a
        # credential *reference*, which is what the review asked to inventory.
        return "credential"
    return None


# --- Value handling. Nothing below ever returns a raw value. ----------------


# The five ways a default can fail to name an external destination. Kept as
# distinct strings rather than a single boolean because "unset", "local only"
# and "deliberate placeholder" are three different operational situations, and
# collapsing them is the same mistake as collapsing the five state columns.
INERT_NOTHING = "yes -- names nothing"
INERT_LOCAL = "yes -- local only, not an external destination"
INERT_PLACEHOLDER = "yes -- development placeholder"
NOT_INERT = "no -- ships pointing somewhere"


def _scheme_and_host(value: str) -> tuple[str, str]:
    """(scheme, host[:port]) with any `user:password@` removed.

    `database_url`'s shipped default carries a password in its userinfo, so
    stripping it is not a nicety. A value with no `://` is not run through
    `urlsplit` for its scheme, because `urlsplit("localhost:7233")` reports the
    scheme as `localhost` -- a wrong answer printed confidently.
    """
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if host and parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed.scheme, host
    bare = re.fullmatch(r"([A-Za-z0-9.\-]+):(\d+)", value.strip())
    if bare:
        return "", f"{bare.group(1)}:{bare.group(2)}"
    return "", ""


def describe_default(value: object, kind: str) -> tuple[str, str]:
    """(characterization of the default, inert?) -- never the value itself."""
    if value is None:
        return "unset (None)", INERT_NOTHING
    if isinstance(value, dict):
        return (
            ("empty map" if not value else f"map of {len(value)} entries"),
            (INERT_NOTHING if not value else NOT_INERT),
        )
    if not isinstance(value, str):
        return UNKNOWN, UNKNOWN
    if value.strip().lower() in PLACEHOLDER_VALUES:
        return ("empty string" if not value else "placeholder literal"), INERT_NOTHING
    if kind == "credential":
        # A credential default is a development placeholder by construction --
        # `reject_insecure_production_configuration` refuses several of them in
        # production -- but its content is never printed regardless.
        return "non-empty placeholder (value not shown)", INERT_PLACEHOLDER
    scheme, host = _scheme_and_host(value)
    if scheme in PLACEHOLDER_SCHEMES:
        return f"placeholder scheme `{scheme}://` (value not shown)", INERT_NOTHING
    if host and host.split(":")[0] in LOCAL_HOSTS:
        return "localhost default (host only shown)", INERT_LOCAL
    if not host:
        # A bare literal that is not a URL or host: a bucket name, a filesystem
        # path, a queue name. It names a thing, but not a reachable address.
        return "bare literal (value not shown)", INERT_NOTHING
    return "names a remote host (host only shown)", NOT_INERT


def describe_target(name: str, value: object, kind: str) -> str:
    """What the setting points at -- scheme and host only, never the value."""
    tokens = _tokens(name)
    if kind == "credential":
        if name.endswith("_key_name"):
            return "the *name* of a key inside the external secret store, not the key"
        if "reference" in tokens:
            return "a secret-store reference resolved by `aida.secrets.SecretResolver`"
        return "a credential held in process configuration"
    if isinstance(value, dict):
        return "operator-supplied alias -> URL map"
    if "bucket" in tokens:
        return "an object-store bucket, named by this setting (name not shown)"
    if "root" in tokens:
        return "a local filesystem path"
    if not isinstance(value, str) or not value:
        return "whatever the deployment supplies -- unset by default"
    scheme, host = _scheme_and_host(value)
    if scheme in PLACEHOLDER_SCHEMES:
        return f"nothing -- `{scheme}://` is a deliberate placeholder scheme"
    if host:
        return f"`{host}`" + (f" over `{scheme}`" if scheme else "")
    return UNKNOWN


# --- Derivation from the codebase ------------------------------------------


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in SRC_ROOT.rglob("*.py")
        if path != CONFIG_MODULE and "__pycache__" not in path.parts
    )


def consumer_index() -> dict[str, list[str]]:
    """setting name -> modules that actually read it as an attribute.

    Attribute reads (`settings.siem_endpoint`, `self.settings.openai_base_url`,
    `loop_settings.audit_archive_bucket_name`) rather than a bare word search,
    so the many docstrings that discuss a setting by name are not mistaken for
    code that uses it. The compatibility shim `aida/config.py` is excluded: it
    re-exports the model and reads nothing.
    """
    index: dict[str, list[str]] = {}
    for path in _python_sources():
        module = ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if module == "aida.config":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                index.setdefault(node.attr, [])
                if module not in index[node.attr]:
                    index[node.attr].append(module)
    return index


def approval_modules() -> set[str]:
    """Modules whose source names a governance-approval marker."""
    named: set[str] = set()
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in APPROVAL_MARKERS):
            named.add(".".join(path.relative_to(SRC_ROOT).with_suffix("").parts))
    return named


def readiness_scope() -> set[str]:
    """Setting names observable from `/health/ready`.

    Deliberately narrow: `aida.readiness` itself plus the `aida.*` modules it
    imports directly. A wider transitive walk would reach most of the package
    through `aida.models` and would claim health coverage that does not exist.
    The known cost of the narrow rule is stated in the generated document.
    """
    if not READINESS_MODULE.is_file():
        return set()
    tree = ast.parse(READINESS_MODULE.read_text(encoding="utf-8"), filename=str(READINESS_MODULE))
    modules = {READINESS_MODULE}
    for node in ast.walk(tree):
        bases: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            bases.append(node.module)
        elif isinstance(node, ast.Import):
            bases.extend(alias.name for alias in node.names)
        for base in bases:
            if not base.startswith("aida"):
                continue
            candidate = SRC_ROOT / Path(*base.split(".")).with_suffix(".py")
            if candidate.is_file():
                modules.add(candidate)
    names: set[str] = set()
    for path in modules:
        try:
            subtree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(subtree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
    return names


def enabling_flag(name: str, boolean_fields: dict[str, bool]) -> tuple[str, bool] | None:
    """The longest-prefix boolean switch that gates this destination.

    `siem_endpoint` -> `siem_enabled`; `audit_archive_bucket_name` ->
    `audit_archive_enabled`; `dq_itsm_webhook_url` -> `dq_itsm_webhook_enabled`.
    Longest prefix wins so `audit_archive_*` is not matched by a shorter,
    unrelated `audit_*` flag.
    """
    tokens = _tokens(name)
    best: tuple[str, bool] | None = None
    best_length = 0
    for flag, default in boolean_fields.items():
        flag_tokens = _tokens(flag)
        if flag_tokens[-1] not in {"enabled", "generation_enabled"}:
            continue
        prefix = flag_tokens[:-1]
        if prefix and tokens[: len(prefix)] == prefix and len(prefix) > best_length:
            best, best_length = (flag, default), len(prefix)
    return best


def collect_rows() -> list[Row]:
    fields = Settings.model_fields
    consumers = consumer_index()
    approvers = approval_modules()
    probed = readiness_scope()
    boolean_fields = {
        name: bool(field.default)
        for name, field in fields.items()
        if _annotation_text(field.annotation) == "<class 'bool'>"
    }

    rows: list[Row] = []
    for name, field in sorted(fields.items()):
        kind = classify(name, field.annotation)
        if kind is None:
            continue
        default = field.default
        if default is None and field.default_factory is not None:
            default = field.default_factory()  # type: ignore[call-arg]
        default_text, inert = describe_default(default, kind)
        modules = consumers.get(name, [])
        flag = enabling_flag(name, boolean_fields)

        rows.append(
            Row(
                setting=f"`{name}`",
                kind=kind,
                points_at=describe_target(name, default, kind),
                default=default_text,
                inert=inert,
                consumed_by=", ".join(f"`{m}`" for m in modules) if modules else UNKNOWN,
                configured=(
                    "no -- the shipped default names nothing"
                    if inert.startswith("yes")
                    else ("ships with a default target" if inert.startswith("no") else UNKNOWN)
                ),
                approved=(
                    UNKNOWN
                    if not modules
                    else (
                        "governance check in the consuming module"
                        if any(m in approvers for m in modules)
                        else "no approval gate found -- whoever sets the variable decides"
                    )
                ),
                active=(
                    f"`{flag[0]}` defaults {'on' if flag[1] else 'off'}"
                    if flag
                    else ("no enabling flag -- used whenever read" if modules else UNKNOWN)
                ),
                healthy=(
                    "probed by `/health/ready`"
                    if name in probed
                    else ("no readiness probe" if modules else UNKNOWN)
                ),
                # Runtime evidence. See the module docstring: nothing static can
                # answer this, and pretending otherwise is the exact defect the
                # review found in the archive and SIEM paths.
                verified=UNKNOWN,
            )
        )
    return rows


def render(rows: list[Row]) -> str:
    unknown_rows = [row for row in rows if row.unknown_cells]
    unknown_cells = sum(len(row.unknown_cells) for row in rows)
    destinations = sum(1 for row in rows if row.kind == "destination")
    credentials = len(rows) - destinations

    lines = [
        "# External destination and credential inventory",
        "",
        "**Generated file. Do not edit by hand.**",
        "Regenerate with `python scripts/generate_destination_inventory.py`;",
        "`--check` fails when this file is out of date with `Settings`.",
        "",
        "Review 2026-09-05, section 6 item 7 asks for an inventory of every external",
        "destination and credential reference, distinguishing **configured**,",
        "**approved**, **active**, **healthy** and **verified**. Those five are not",
        "synonyms, and the whole value of this table is that it refuses to treat them",
        "as one. Every row is derived from `atlas.platform.config.Settings` and from a",
        "static read of how `src/` consumes it. Nothing here is hand-maintained.",
        "",
        "## No value from this configuration appears in this file",
        "",
        "Several of the settings below are credentials, and `database_url`'s shipped",
        "default carries a password in its userinfo. The generator therefore prints",
        "the setting **name**, a **characterization** of its default (`unset`,",
        "`empty string`, `placeholder`, `localhost`, ...) and, for destinations, the",
        "**host and scheme only**, with any `user:password@` removed. No setting's",
        "value is written here -- not a credential's, not a URL's, not even a default",
        "that is already visible in `config.py`. Read `config.py` for the defaults;",
        "read the deployment's secret store for the values.",
        "",
        "## What each state column means",
        "",
        "- **Configured** -- does the software ship naming a real destination? This",
        "  generator reads source, not a deployment's environment, so it answers that",
        "  question and no other. A deployment that sets the variable is configured;",
        "  this table cannot see that and does not claim to.",
        "- **Approved** -- does a governance decision gate the use, or does whoever",
        "  set the environment variable decide? Derived from whether the consuming",
        "  module names an approval marker (`APPROVED`, `GovernanceReview`,",
        "  `evaluate_entitlement`, `authorize_enforced`).",
        "- **Active** -- the feature flag that decides whether the code opens the",
        "  connection at all, and which way it defaults. Almost every outbound path",
        "  here is off by default, deliberately (see the review's F01/F04 notes);",
        "  that is a different fact from being unconfigured.",
        "- **Healthy** -- whether `/health/ready` observes it. Derived from",
        "  `aida.readiness` and the `aida.*` modules it imports directly.",
        "- **Verified** -- whether a real destination has acknowledged real traffic.",
        "",
        "## What this analysis cannot see",
        "",
        "- **Every `Verified` cell is `unknown`, and that is the correct answer.**",
        "  Verification is runtime evidence: a bucket that stored bytes and handed",
        "  them back, a collector that acknowledged an event. No static generator can",
        "  produce it. This is the same answer F01, F04 and section 6 item 8 give --",
        "  filesystem archival and loopback delivery are proven; a real object-lock",
        "  bucket and a real SOC collector are not.",
        "- A deployment's actual environment is invisible here. `Configured` is a",
        "  statement about the shipped default only.",
        "- `Healthy` uses a deliberately narrow one-hop scope. A probe handed an",
        "  already-constructed client (`probe_temporal` receives its client from",
        "  `aida.main`'s lifespan) therefore reads as `no readiness probe` against the",
        "  *address setting*, even though the service itself is probed. A wider",
        "  transitive walk would reach most of the package through `aida.models` and",
        "  would claim health coverage that does not exist; over-claiming is the worse",
        "  error of the two.",
        "- A destination reached through a dynamically-built string, or configured",
        "  per-organization in a database row rather than in `Settings`, is not a",
        "  field on this model and is therefore not in this table.",
        "",
        "## Coverage",
        "",
        f"- Settings inventoried: **{len(rows)}** "
        f"({destinations} destinations, {credentials} credential references)",
        f"- Rows with at least one `unknown` cell: **{len(unknown_rows)}**",
        f"- `unknown` cells in total: **{unknown_cells}** "
        f"(of which {len(rows)} are the `Verified` column, by construction)",
        "",
    ]

    if unknown_rows:
        lines += [
            "### Gap list -- cells the analysis could not determine",
            "",
            "| Setting | Undetermined cells |",
            "|---|---|",
        ]
        for row in unknown_rows:
            lines.append(f"| {row.setting} | {', '.join(row.unknown_cells)} |")
        lines.append("")

    lines += [
        "## Inventory",
        "",
        "| Setting | Kind | Points at | Default | Default inert? | Consumed by | "
        "Configured | Approved | Active | Healthy | Verified |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.setting} | {row.kind} | {row.points_at} | {row.default} | {row.inert} | "
            f"{row.consumed_by} | {row.configured} | {row.approved} | {row.active} | "
            f"{row.healthy} | {row.verified} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the committed file differs from what would be generated.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args()

    rows = collect_rows()
    content = render(rows)

    if args.stdout:
        print(content)
        return 0
    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run without --check to create it.")
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            print(f"{args.output} is out of date with Settings; regenerate it.")
            return 1
        print(f"{args.output} is up to date ({len(rows)} settings).")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    unknown_cells = sum(len(row.unknown_cells) for row in rows)
    print(
        f"wrote {args.output} -- {len(rows)} settings, {unknown_cells} unknown cells",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
