#!/usr/bin/env python3
"""R11-S9 step one: an inventory of every setting, what it defaults to, and
whether anything reads it.

The row asks to "inventory supported/default-off features; enable with evidence
or retire deliberately". The inventory has to come first and it has to be
generated, because a hand-written one is wrong within a week — this is a
`Settings` class with over two hundred fields, and the reason the row exists is
that nobody can say from memory which of them do anything.

Three questions, and they are different:

**Does anything read it?** A field nothing reads is dead configuration. It is
not a feature that is switched off; it is a promise the code does not keep, and
an operator who sets it gets silence. These are the retirement candidates and
they are listed first, because they are the only group where the right action
is unambiguous.

**Is it off by default?** `False`, `None`, `""`, `0`, an empty collection. Off
by default is a legitimate and common state here — the platform ships safe —
but the register's own rule is that a setting which exists and defaults to off
does not earn a "Configured: Yes", so this list is what that claim has to be
checked against.

**How many places read it?** A setting read in one place is a local switch. One
read in twenty places is load-bearing, and retiring it is a different
conversation. The count is reported so that conversation starts from a number.

What this deliberately does **not** do is decide. "Enable with evidence or
retire deliberately" is a judgement per feature, and a script that proposed
retirements would be read as having made them.

Counting method, and its one honest weakness: a read is an attribute access
named after the field anywhere under `src/` outside the config module itself,
found by walking the AST. That over-counts when an unrelated object happens to
carry an attribute of the same name (`name`, `environment`), so a *high* count
is a hint and a *zero* is the reliable signal — nothing in the tree accesses
that name at all. Zero is the number this inventory exists to surface.

Usage:
    python scripts/generate_configuration_inventory.py            # write
    python scripts/generate_configuration_inventory.py --check    # verify
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "src/atlas/platform/config.py"
SRC_ROOT = REPO_ROOT / "src"
OUTPUT_PATH = REPO_ROOT / "Docs/40-engineering/13-configuration-inventory.md"

#: Fields whose name is too common to count attribute accesses for. They are
#: still inventoried; only their read count is reported as unknown, because a
#: number nobody can trust is worse than an honest blank.
AMBIGUOUS_NAMES = frozenset(
    {
        "name",
        "environment",
        "status",
        "timeout",
        "url",
        "version",
        "provider",
        "enabled",
        "region",
        "endpoint",
    }
)


@dataclass(frozen=True, slots=True)
class SettingField:
    name: str
    annotation: str
    default: str
    reads: int | None
    #: Reached through `getattr(settings, f"{...}_suffix")` rather than by name.
    dynamic: bool = False
    #: The `Settings` member that exposes this field, when nothing reads the
    #: field directly but something reads that member.
    via: str = ""

    @property
    def off_by_default(self) -> bool:
        """Whether the shipped default leaves this doing nothing.

        `0` counts: this codebase uses a zero interval to mean "never run",
        which is the task-agent convention, so a zero-defaulted interval is
        off in exactly the sense the row means.
        """
        return self.default in {"False", "None", '""', "''", "0", "0.0", "{}", "[]", "dict", "list"}


def _unparse(node: ast.expr | None) -> str:
    if node is None:
        return ""
    return ast.unparse(node)


def _default_of(node: ast.AnnAssign) -> str:
    """The shipped default, normalised.

    `Field(default_factory=dict)` and `Field(default=None, ...)` are the two
    wrappers used here; both are unwrapped so that "empty by default" is
    visible rather than hidden behind a call expression.
    """
    if node.value is None:
        return "required"
    value = node.value
    if isinstance(value, ast.Call) and _unparse(value.func).endswith("Field"):
        for keyword in value.keywords:
            if keyword.arg == "default_factory":
                return _unparse(keyword.value)
            if keyword.arg == "default":
                return _unparse(keyword.value)
        if value.args:
            return _unparse(value.args[0])
        return "required"
    return _unparse(value)


def _settings_fields() -> list[tuple[str, str, str]]:
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"), filename=str(CONFIG_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return [
                (statement.target.id, _unparse(statement.annotation), _default_of(statement))
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and not statement.target.id.startswith("_")
                and statement.target.id != "model_config"
            ]
    raise SystemExit("class Settings not found in src/atlas/platform/config.py")


def _config_member_reads() -> dict[str, set[str]]:
    """Which `Settings` members read which fields, as `field -> {members}`.

    A property like `max_query_estimate_cost` returning `self.max_postgres_plan_cost`
    is the field's real consumer, and the field is live exactly when that member
    is. Validators appear here too and are harmless: a validator with no
    external readers contributes a zero, so a field only a validator touches
    still reads as unused.
    """
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"), filename=str(CONFIG_PATH))
    exposures: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(member):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id in {"self", "cls"}
                ):
                    exposures.setdefault(inner.attr, set()).add(member.name)
    return exposures


def _attribute_reads() -> Counter[str]:
    """Every attribute access under `src/`, except inside the config module.

    The config module is excluded because a field referenced by its own
    validator is not a consumer of the setting -- counting it would make every
    cross-validated field look used.
    """
    counts: Counter[str] = Counter()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == CONFIG_PATH:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file is not this gate's problem
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                counts[node.attr] += 1
    return counts


#: A literal shorter than this is too generic to treat as naming a setting.
#: `"_id"` would mark a third of the class dynamic and make the column useless.
MIN_FRAGMENT = 6

_FRAGMENT = re.compile(r"^[a-z0-9_]+$")


def _module_constants() -> dict[str, str]:
    """Module-level `NAME = "literal"` strings across `src/`.

    Needed because the idiom that actually matters here is indirect:
    `f"{self.key}{_PRINCIPAL_SETTING_SUFFIX}"` and
    `name.endswith(_PRINCIPAL_SETTING_SUFFIX)`. Without resolving the constant,
    the f-string degenerates to "any name" and is discarded, and three live
    settings are reported as dead.
    """
    constants: dict[str, str] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for statement in tree.body:
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                targets = list(statement.targets)
                value = statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets = [statement.target]
                value = statement.value
            else:
                continue
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _literal_of(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _dynamic_name_patterns() -> list[re.Pattern[str]]:
    """Regexes for every construction under `src/` that could name a setting.

    Two idioms, both real in this tree:

    * an f-string -- literal parts become themselves, interpolations become
      `[a-z0-9_]*`. A pattern with too little literal text is dropped: it would
      match every field and turn the dynamic column into noise;
    * `name.endswith(SUFFIX)` / `startswith(PREFIX)` over `model_fields`, which
      is how `reserved_principals` finds every *other* agent's identity
      setting. That is a read, and a walker blind to it reports the settings it
      finds as unused.
    """
    constants = _module_constants()
    patterns: list[re.Pattern[str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == CONFIG_PATH:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                parts: list[str] = []
                literal_chars = 0
                for value in node.values:
                    literal = (
                        value.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                        else (
                            _literal_of(value.value, constants)
                            if isinstance(value, ast.FormattedValue)
                            else None
                        )
                    )
                    if literal is not None:
                        parts.append(re.escape(literal))
                        literal_chars += len(literal)
                    else:
                        parts.append(r"[a-z0-9_]*")
                if literal_chars >= MIN_FRAGMENT:
                    patterns.append(re.compile(f"^{''.join(parts)}$"))
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"endswith", "startswith"}
                and len(node.args) == 1
            ):
                literal = _literal_of(node.args[0], constants)
                if literal is None or len(literal) < MIN_FRAGMENT:
                    continue
                if not _FRAGMENT.match(literal):
                    continue
                escaped = re.escape(literal)
                patterns.append(
                    re.compile(
                        f"^[a-z0-9_]*{escaped}$"
                        if node.func.attr == "endswith"
                        else f"^{escaped}[a-z0-9_]*$"
                    )
                )
    return patterns


def _literal_names() -> set[str]:
    """Plain string constants under `src/` -- the `getattr(settings, "x")` case."""
    names: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == CONFIG_PATH:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
    return names


def collect() -> list[SettingField]:
    reads = _attribute_reads()
    patterns = _dynamic_name_patterns()
    literals = _literal_names()
    exposures = _config_member_reads()
    fields: list[SettingField] = []
    for name, annotation, default in _settings_fields():
        static = None if name in AMBIGUOUS_NAMES else reads.get(name, 0)
        dynamic = False
        via = ""
        if static == 0:
            dynamic = name in literals or any(pattern.match(name) for pattern in patterns)
            if not dynamic:
                # Followed one hop, not transitively: a member exposing a field
                # while itself being unread is the same dead chain, and saying
                # "read via something nothing reads" would be noise.
                exposed_by = sorted(
                    member for member in exposures.get(name, set()) if reads.get(member, 0) > 0
                )
                if exposed_by:
                    via = exposed_by[0]
                    static = reads[via]
        fields.append(
            SettingField(
                name=name,
                annotation=annotation,
                default=default,
                reads=static,
                dynamic=dynamic,
                via=via,
            )
        )
    return fields


def _row(field: SettingField) -> str:
    if field.reads is None:
        reads = "not counted"
    elif field.dynamic:
        reads = "dynamic"
    elif field.via:
        reads = f"{field.reads} via `{field.via}`"
    else:
        reads = str(field.reads)
    default = field.default if field.default != "required" else "**required**"
    return f"| `{field.name}` | `{field.annotation}` | `{default}` | {reads} |"


def render(fields: list[SettingField]) -> str:
    unread = [f for f in fields if f.reads == 0 and not f.dynamic]
    off = [f for f in fields if f.off_by_default and f not in unread]
    lines = [
        "<!-- GENERATED by scripts/generate_configuration_inventory.py -- do not edit by hand. -->",
        "",
        "# Configuration inventory",
        "",
        "> Generated from `src/atlas/platform/config.py`. Regenerate with",
        "> `python scripts/generate_configuration_inventory.py`; "
        "`tests/test_configuration_inventory.py` fails when it is stale.",
        "",
        "R11-S9's first clause: *inventory supported/default-off features; enable with",
        "evidence or retire deliberately.* This is the inventory. It decides nothing —",
        "\"enable or retire\" is a judgement per feature, and a generated file that",
        "proposed retirements would be read as having made them.",
        "",
        "**How to read the Reads column.** It counts attribute accesses of that name",
        "anywhere under `src/` outside the config module itself. A *high* count is a",
        "hint, because an unrelated object may carry an attribute of the same name; a",
        "*zero* is the reliable signal, because nothing in the tree accesses the name at",
        "all. Ten field names are too common to count at all and say so. A setting reached",
        "through `getattr(settings, f\"{key}_suffix\")` -- how the task agents read theirs --",
        "reads **dynamic**; one exposed by a property on `Settings` itself shows that",
        "member's count and `via`. Neither is a retirement candidate.",
        "",
        f"**{len(fields)} settings.** {len(unread)} are read nowhere. "
        f"{len(off)} more ship switched off, empty or zero.",
        "",
        "## 1. Read by nothing",
        "",
        "A field nothing reads is not a feature that is switched off — it is a promise",
        "the code does not keep. An operator who sets one of these gets silence. These",
        "are the retirement candidates, and the only group where the action is",
        "unambiguous.",
        "",
    ]
    if unread:
        lines += ["| Setting | Type | Default | Reads |", "|---|---|---|---|"]
        lines += [_row(field) for field in unread]
    else:
        lines += ["*None: every setting is read somewhere.*"]
    lines += [
        "",
        "## 2. Shipped off, empty or zero",
        "",
        "A legitimate and common state — this platform ships safe, and a zero interval",
        "means *never* by the task-agent convention. It is listed because the capability",
        "register's own rule is that a setting which exists and defaults to off does not",
        "earn a *Configured: Yes*, so this is the list that claim must be checked against.",
        "",
        "| Setting | Type | Default | Reads |",
        "|---|---|---|---|",
    ]
    lines += [_row(field) for field in off]
    lines += [
        "",
        "## 3. Every setting",
        "",
        "| Setting | Type | Default | Reads |",
        "|---|---|---|---|",
    ]
    lines += [_row(field) for field in fields]
    lines += [""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the committed file is stale, writing nothing",
    )
    args = parser.parse_args()
    rendered = render(collect())
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(f"{OUTPUT_PATH} is out of date with src/atlas/platform/config.py; regenerate it.")
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
