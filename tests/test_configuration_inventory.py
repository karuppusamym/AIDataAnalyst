"""R11-S9: the configuration inventory stays current, and dead settings cannot land.

`Docs/40-engineering/13-configuration-inventory.md` is generated from the
`Settings` class. A generated document nothing checks is a stale document with
extra steps, so this keeps it honest and adds the one ratchet the row's own
wording implies: *enable with evidence or retire deliberately* means a setting
that does neither should not exist, and today none does.

The zero ratchet is the point. It is trivially satisfiable right now and stays
that way only if every new setting is wired to something before it lands, which
is the discipline the row asks for and the cheapest moment to apply it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_configuration_inventory import (  # noqa: E402
    OUTPUT_PATH,
    collect,
    render,
)


def test_the_committed_inventory_matches_the_settings_class() -> None:
    """Regenerate with `python scripts/generate_configuration_inventory.py`."""
    assert OUTPUT_PATH.exists(), f"{OUTPUT_PATH} is missing; regenerate it"
    assert OUTPUT_PATH.read_text(encoding="utf-8") == render(collect()), (
        f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is stale with respect to "
        "src/atlas/platform/config.py. Regenerate it with "
        "`python scripts/generate_configuration_inventory.py` and commit the result."
    )


def test_the_inventory_is_not_vacuous() -> None:
    """A tripwire. If the parser stops finding fields, the ratchet below passes
    for the wrong reason -- zero dead settings out of zero settings."""
    fields = collect()
    assert len(fields) >= 200, (
        f"only {len(fields)} settings parsed from the Settings class; the class layout "
        "has changed and both the inventory and the ratchet below have stopped checking"
    )


def test_no_setting_is_read_by_nothing() -> None:
    """The ratchet.

    A field nothing reads is not a feature that is off -- it is a promise the
    code does not keep, and an operator who sets it gets silence. Three
    indirections count as reads and are resolved by the generator: dynamic
    `getattr(settings, f"{key}_suffix")`, a `model_fields` walk filtering on a
    suffix constant, and a property on `Settings` that exposes the field to
    everyone else. If this fails, wire the setting or delete it; do not add an
    exemption.
    """
    unread = [field.name for field in collect() if field.reads == 0 and not field.dynamic]
    assert not unread, (
        "these settings are read nowhere under src/ -- wire them to something or "
        "remove them, rather than shipping configuration that silently does nothing:\n"
        + "\n".join(f"  - {name}" for name in unread)
    )
