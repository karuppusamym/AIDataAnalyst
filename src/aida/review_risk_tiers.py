"""ADR-0027: deterministic risk tiers for governance review items.

Every `GovernanceReview` object type is classified into one of four tiers.
The tier decides two things:

* whether the reviewer agent (`aida.reviewer_agent`) may decide the item at
  all -- capped at `AIDA_REVIEWER_AGENT_MAX_TIER` (default `T1`) and then
  clamped again to `HARD_MAX_AGENT_TIER`, which configuration cannot raise;
  and
* how the agent inbox ranks and labels the item for a human.

The classification is a pure function of the object type -- no model, no
lookup, no configuration -- so the same proposal always lands in the same
tier and the tier can be recomputed from an audit row years later.

**Unknown types are T3.** A type this module has never heard of is the exact
case where guessing is dangerous, so it gets the tier no agent may ever
decide. Adding a new governed object type therefore defaults to human-only
review until someone deliberately classifies it here, which is the correct
direction for a fail-closed platform (INV-4).

The tiers, stated as the question they answer:

* **T0** -- "if this is wrong, someone reads a slightly worse sentence."
  Language attached to an asset. Reversible by editing text.
* **T1** -- "if this is wrong, a link or a label is wrong." Structural
  attachments between existing objects, and bulk operations small enough
  that the governance threshold already lets a steward apply them directly.
  Reversible by unlinking.
* **T2** -- "if this is wrong, a published definition or an executable
  capability is wrong." Semantic versions, governed tools, data products,
  contracts. Reversible only by publishing a correction.
* **T3** -- "if this is wrong, the platform's trust boundary moved."
  Policy, access, model routes, agent registrations, cross-boundary grants.
  Never agent-decidable, whatever the configuration says.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

RiskTier = str

TIER_T0: Final = "T0"
TIER_T1: Final = "T1"
TIER_T2: Final = "T2"
TIER_T3: Final = "T3"

TIER_ORDER: Final[tuple[str, ...]] = (TIER_T0, TIER_T1, TIER_T2, TIER_T3)

#: The default ceiling for the reviewer agent. Deliberately excludes T2 and
#: T3 (ADR-0027 condition (a)): an agent may never publish a semantic
#: version, activate a model route, change a policy, or grant access.
DEFAULT_MAX_AGENT_TIER: Final = TIER_T1

#: The ceiling no configuration can raise (AR-01). ADR-0027 condition (a) is
#: stated as an unconditional boundary -- "never agent-decidable, whatever the
#: configuration says" -- but until 2026-09-09 it was only a *default*:
#: `Settings.reviewer_agent_max_tier` accepts T2 and T3, and
#: `agent_decidable_object_types` passed that value straight into the
#: comparison, so an elevated ceiling widened the allowlist to include
#: `CONTEXT_PRODUCT_VERSION`, `MODEL_ROUTE_CONFIGURATION` and every other
#: published-meaning or trust-boundary type. This constant is the invariant
#: the ADR always claimed: configuration narrows, never widens.
HARD_MAX_AGENT_TIER: Final = TIER_T1


def effective_agent_ceiling(configured: RiskTier | None) -> RiskTier:
    """The ceiling actually in force, given what configuration asked for.

    Clamped to `HARD_MAX_AGENT_TIER` rather than rejected, because the
    alternative -- refusing to start on a T2/T3 value -- turns a
    misconfiguration into an outage for a feature that is off by default.
    Callers that need to *report* the difference compare this against the
    configured value; `reviewer_agent` writes both into its evidence so an
    auditor can see that a widened ceiling was asked for and refused.

    An unrecognised or absent value falls back to the default, not to the
    configured string: a ceiling the tier order cannot compare would make
    `tier_at_or_below` answer False for everything, which is safe but silent.
    """
    if configured not in TIER_ORDER:
        return DEFAULT_MAX_AGENT_TIER
    if TIER_ORDER.index(configured) > TIER_ORDER.index(HARD_MAX_AGENT_TIER):
        return HARD_MAX_AGENT_TIER
    return configured

_TIERS: Final[Mapping[str, str]] = {
    # --- T0: language attached to an asset -------------------------------
    "ASSET_DESCRIPTION_DRAFT": TIER_T0,
    "COLUMN_DESCRIPTION_DRAFT": TIER_T0,
    "ASSET_DOCUMENTATION_VERSION": TIER_T0,
    "BUSINESS_ANNOTATION": TIER_T0,
    "METADATA_ENRICHMENT_PROPOSAL": TIER_T0,
    # --- T1: structural attachments between existing objects -------------
    "GLOSSARY_LINK_PROPOSAL": TIER_T1,
    "TERM_SEMANTIC_BINDING": TIER_T1,
    "COLUMN_CLASSIFICATION_PROMOTION": TIER_T1,
    "QUERY_HISTORY_METRIC_CANDIDATE": TIER_T1,
    "DOCUMENT_CLAIM": TIER_T1,
    # Bulk stewardship is T1 only below the governance threshold; see
    # `risk_tier_for`, which reads the item count out of the payload.
    "BULK_STEWARDSHIP_OPERATION": TIER_T1,
    # A workbook import is bulk language, so it takes the same shape as bulk
    # stewardship above: T1 at a size a steward could plausibly have typed by
    # hand, T2 beyond it. A 900-row import is not a bigger version of a small
    # one -- nobody reads 900 rows carefully, which is exactly why an agent
    # must not wave it through.
    "MODEL_IMPORT_BATCH": TIER_T1,
    # --- T2: published meaning and executable capability ------------------
    # Retiring a description is deliberately *above* the tier of publishing
    # one (`ASSET_DOCUMENTATION_VERSION`, T0). The asymmetry is the point: a
    # withdrawal undoes a decision a human already made and removes grounding
    # an agent run may have cited. Publishing bad language is visible and
    # correctable; silently un-publishing good language is neither, and an
    # agent should never be the one to do it unattended.
    "DESCRIPTION_WITHDRAWAL": TIER_T2,
    # --- T2: published meaning and executable capability ------------------
    "SEMANTIC_MODEL_VERSION": TIER_T2,
    "SEMANTIC_METRIC": TIER_T2,
    "SEMANTIC_METRIC_PROPOSAL": TIER_T2,
    "GLOSSARY_TERM": TIER_T2,
    "GLOSSARY_TERM_VERSION": TIER_T2,
    "GLOSSARY_CONFLICT": TIER_T2,
    "GOVERNED_TOOL": TIER_T2,
    "GOVERNED_TOOL_VERSION": TIER_T2,
    "TOOL_CERTIFICATION_RUN": TIER_T2,
    "CONTEXT_PRODUCT_VERSION": TIER_T2,
    "DATA_PRODUCT_VERSION": TIER_T2,
    "DATA_CONTRACT_VERSION": TIER_T2,
    # A quality rule is an executable control: its incidents gate governed
    # tools, demote retrieval and attach trust warnings to answers
    # (`custom_quality_rules`). A wrong threshold switches a capability off or
    # hides a regression -- the harm a data contract's quality clause can do,
    # so the same tier. The quality agent proposes these (ADR-0029); no agent
    # decides them.
    "QUALITY_RULE_PROPOSAL": TIER_T2,
    # A published ontology version is published meaning -- the concepts, the
    # relations between them and the tables they map to -- the same kind of
    # change as a glossary term version.
    "ONTOLOGY_VERSION": TIER_T2,
    # --- T3: the trust boundary itself ------------------------------------
    "MODEL_ROUTE_CONFIGURATION": TIER_T3,
    "AI_ASSET": TIER_T3,
    "AI_ASSET_VERSION": TIER_T3,
    "AGENT_CONTRACT": TIER_T3,
    "AGENT_CONTRACT_REQUEST": TIER_T3,
    "CROSS_BOUNDARY_GRANT": TIER_T3,
    "DATA_PRODUCT_ACCESS_REQUEST": TIER_T3,
    "ACCESS_POLICY": TIER_T3,
    "SOURCE_BINDING": TIER_T3,
    "WORKSPACE_MEMBERSHIP": TIER_T3,
}


#: Object types whose tier depends on how much they change, not only on what
#: they are. Both are bulk paths where size is the risk.
_COUNT_ESCALATED: Final[frozenset[str]] = frozenset(
    {"BULK_STEWARDSHIP_OPERATION", "MODEL_IMPORT_BATCH"}
)


def risk_tier_for(object_type: str, payload: Mapping[str, Any] | None = None) -> RiskTier:
    """The tier for one review item. Unknown types are `T3` (fail closed).

    `payload` is optional evidence about the specific item. Two rules use it,
    both the same idea: a bulk change is T1 at the size a steward could have
    applied directly, and T2 above it -- the same threshold
    `AIDA_BULK_GOVERNANCE_THRESHOLD` uses to decide whether the operation
    needed review in the first place. An operation large enough that the
    platform insisted on a human is not one an agent should wave through.

    `BULK_STEWARDSHIP_OPERATION` counts items; `MODEL_IMPORT_BATCH` counts the
    changes a workbook would publish. They are the same question asked of two
    different bulk paths, so they escalate the same way rather than growing
    two thresholds that could drift.
    """
    tier = _TIERS.get(object_type)
    if tier is None:
        return TIER_T3
    if object_type in _COUNT_ESCALATED and payload is not None:
        count = (
            payload.get("item_count")
            or payload.get("change_count")
            or payload.get("applied_count")
            or 0
        )
        threshold = payload.get("governance_threshold", 10)
        try:
            if int(count) > int(threshold):
                return TIER_T2
        except (TypeError, ValueError):
            # An unparseable count is not evidence of smallness.
            return TIER_T2
    return tier


def tier_at_or_below(tier: RiskTier, ceiling: RiskTier) -> bool:
    """Whether `tier` is within `ceiling`, by the T0 < T1 < T2 < T3 order.

    An unrecognised tier on either side answers False: the caller is asking
    "may an agent act here", and the safe answer to a question posed in
    terms it does not understand is no.
    """
    if tier not in TIER_ORDER or ceiling not in TIER_ORDER:
        return False
    return TIER_ORDER.index(tier) <= TIER_ORDER.index(ceiling)


def agent_decidable_object_types(ceiling: RiskTier = DEFAULT_MAX_AGENT_TIER) -> frozenset[str]:
    """Every object type an agent may decide at `ceiling`.

    The ceiling is clamped to `HARD_MAX_AGENT_TIER` before it is used, so a
    configured T2 or T3 narrows nothing and widens nothing: the result is the
    T0/T1 set either way. This is the fix for AR-01 -- the docstring here
    always claimed configuration "can never widen it", but the claim rested on
    the caller passing a value no larger than T1, which nothing enforced.
    """
    in_force = effective_agent_ceiling(ceiling)
    return frozenset(
        object_type
        for object_type, tier in _TIERS.items()
        if tier_at_or_below(tier, in_force)
    )


def requires_size_evidence(object_type: str) -> bool:
    """Whether this type's tier cannot be settled from the type alone (AR-02).

    `risk_tier_for` answers T1 for both bulk types when it is handed no
    payload, which reads as "small" when it actually means "not measured".
    Callers that gate an *action* on the tier -- as opposed to labelling a
    queue row -- must resolve an authoritative count first, and treat an
    unresolvable one as T2. Exposed as a predicate so those callers do not
    each hard-code the pair.
    """
    return object_type in _COUNT_ESCALATED


def known_object_types() -> frozenset[str]:
    """Every object type this module classifies. Used by the test that keeps
    the table in step with the object types the codebase actually creates."""
    return frozenset(_TIERS)
