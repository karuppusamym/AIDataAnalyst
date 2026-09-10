"""F11: the workspace-authorization posture, made explicit, checkable and visible.

The review's finding was not that workspace authorization is off. It is that
nothing anywhere says whether it is on. `Settings.unresolved_workspace_posture`
defaults to SHADOW (a request whose workspace cannot be resolved proceeds
undecided), individual workspaces default to SHADOW (`Workspace.
authorization_mode`), production settings validation checked neither, and
`/health/ready` reported dependencies rather than controls -- so a deployment
could satisfy every automated check while enforcing nothing at the workspace
level, and the only way to find out was to read `authorization_gate.gate`.

This module is the answer to "is this control enforcing?" and it distinguishes
three states, because collapsing them is what made the finding possible:

``ENFORCING``
    The deployment claims enforcement (`Settings.workspace_authorization_posture`
    is ENFORCING), unresolved-scope requests are DENIED, and every ACTIVE
    workspace is in ENFORCE. A denial is a real outcome here.
``OBSERVING``
    The control computes the full decision and records divergence but does not
    deny -- either because the deployment is still in the ADR-0018 migration
    (posture OBSERVING, today's default) or because some workspaces are still in
    SHADOW. Nothing is being enforced at the workspace level. This is a
    legitimate state; it is only a problem when something claims otherwise.
``SCOPE_UNRESOLVED``
    The control could not determine its own scope -- the workspace inventory was
    unreadable (database down, timed out). This is deliberately NOT reported as
    OBSERVING: "not enforcing" and "cannot tell whether it is enforcing" are
    different operational facts and an operator must be able to tell them apart.

**The default is deliberately unchanged.** `workspace_authorization_posture`
defaults to OBSERVING and `unresolved_workspace_posture` still defaults to
SHADOW. F11's remediation is to make the posture explicit and verifiable, not to
flip it -- tightening is an intentional migration whose steps are documented on
`Settings.workspace_authorization_posture`. What is new is that a deployment
which *claims* ENFORCING can no longer be wrong about it: the claim fails
settings validation if unresolved scope would be allowed through, and fails
process startup if any ACTIVE workspace is still observing.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import Workspace
from atlas.platform.config import Settings

# The control this module reports on, as it appears in `/health/ready`'s
# `controls` map. One name, so an operator grepping a dashboard and an engineer
# grepping the source find the same string.
CONTROL_NAME = "workspace_authorization"

# The three states. See the module docstring for what each one asserts.
ENFORCING = "ENFORCING"
OBSERVING = "OBSERVING"
SCOPE_UNRESOLVED = "SCOPE_UNRESOLVED"

# What a request whose workspace could not be resolved actually experiences.
# `authorization_gate.gate` is the code this mirrors; keeping the vocabulary
# separate from the setting's SHADOW/DENY makes the report readable without
# knowing the setting.
UNRESOLVED_DENIED = "DENIED"
UNRESOLVED_PROCEEDS_UNDECIDED = "PROCEEDS_UNDECIDED"

# `Workspace.status` values a posture claim has to account for. A DELETED or
# ARCHIVED workspace serves no traffic, so leaving it in SHADOW is not a hole.
_LIVE_WORKSPACE_STATUS = "ACTIVE"


class PostureViolation(RuntimeError):
    """A deployment claims ENFORCING but the inspected state contradicts it.

    Raised only from `assert_startup_posture`, and only when the contradiction
    was actually observed -- never when the inventory could not be read, which
    is `SCOPE_UNRESOLVED` and a readiness signal, not a startup failure.
    """


@dataclass(frozen=True, slots=True)
class PostureReport:
    """What workspace authorization is doing right now, and how we know.

    `state` is the one-word answer an operator reads. Everything else exists so
    that answer can be checked rather than believed: `problems` is empty exactly
    when `declared_posture` matches what the inspected state supports.
    """

    state: str
    declared_posture: str
    unresolved_scope_outcome: str
    # `None` when the workspace inventory could not be read (state is then
    # SCOPE_UNRESOLVED). Zero is a real answer and means "no ACTIVE workspaces".
    workspaces_total: int | None
    workspaces_enforcing: int | None
    workspaces_observing: int | None
    problems: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        """True when nothing contradicts what the deployment declares."""
        return not self.problems

    def as_signals(self) -> dict[str, str]:
        """Flat string map for `/health/ready`, which reports strings only.

        Counts are stringified rather than omitted: "enforcing 0 of 12
        workspaces" is the number an operator needs, and a health endpoint that
        reports only a verdict is how the original finding stayed invisible.
        """
        signals = {
            f"{CONTROL_NAME}.declared": self.declared_posture,
            f"{CONTROL_NAME}.unresolved_scope": self.unresolved_scope_outcome,
        }
        if self.workspaces_total is not None:
            signals[f"{CONTROL_NAME}.workspaces_total"] = str(self.workspaces_total)
            signals[f"{CONTROL_NAME}.workspaces_enforcing"] = str(self.workspaces_enforcing)
            signals[f"{CONTROL_NAME}.workspaces_observing"] = str(self.workspaces_observing)
        if self.problems:
            signals[f"{CONTROL_NAME}.problems"] = "; ".join(self.problems)
        return signals


def _unresolved_scope_outcome(settings: Settings) -> str:
    return (
        UNRESOLVED_DENIED
        if settings.unresolved_workspace_posture == "DENY"
        else UNRESOLVED_PROCEEDS_UNDECIDED
    )


def describe_configured_posture(settings: Settings) -> PostureReport:
    """The posture derivable from configuration alone -- no database.

    Used where a DB round trip is not available or not wanted (a worker's
    startup log, a settings-only test). It can prove a deployment is OBSERVING
    but never that one is ENFORCING, because the workspace inventory is the
    other half of that claim; a configured-only report therefore reports
    ENFORCING with `workspaces_total=None`, and callers that need the verified
    answer use `evaluate_posture`.
    """
    unresolved = _unresolved_scope_outcome(settings)
    declared = settings.workspace_authorization_posture
    problems: list[str] = []
    if declared == ENFORCING and unresolved != UNRESOLVED_DENIED:
        # Unreachable through `Settings` (its validator rejects this pairing);
        # kept because this function also runs against hand-built settings
        # doubles in tests, where the validator is not the only thing standing
        # between a wrong claim and a report that repeats it.
        problems.append("declares ENFORCING but unresolved-workspace requests proceed undecided")
    state = ENFORCING if declared == ENFORCING and not problems else OBSERVING
    return PostureReport(
        state=state,
        declared_posture=declared,
        unresolved_scope_outcome=unresolved,
        workspaces_total=None,
        workspaces_enforcing=None,
        workspaces_observing=None,
        problems=tuple(problems),
    )


async def evaluate_posture(session: AsyncSession, settings: Settings) -> PostureReport:
    """The verified posture: configuration plus the actual workspace inventory.

    One grouped query, so this is safe to call from a readiness probe on every
    scrape. The caller is responsible for bounding it (`asyncio.wait_for`) and
    for turning a failure into `unresolvable_posture` -- this function assumes a
    working session and does not swallow database errors, because a readiness
    probe that reports OBSERVING when it actually failed to look is the same
    class of bug as the one F11 found.
    """
    rows = (
        await session.execute(
            select(Workspace.authorization_mode, func.count())
            .where(Workspace.status == _LIVE_WORKSPACE_STATUS)
            .group_by(Workspace.authorization_mode)
        )
    ).all()
    by_mode = {str(mode): int(count) for mode, count in rows}
    enforcing = by_mode.get("ENFORCE", 0)
    total = sum(by_mode.values())
    observing = total - enforcing

    configured = describe_configured_posture(settings)
    problems = list(configured.problems)
    if configured.declared_posture == ENFORCING and observing:
        problems.append(
            f"declares ENFORCING but {observing} of {total} ACTIVE workspaces are not in ENFORCE"
        )
    state = ENFORCING if configured.declared_posture == ENFORCING and not problems else OBSERVING
    return PostureReport(
        state=state,
        declared_posture=configured.declared_posture,
        unresolved_scope_outcome=configured.unresolved_scope_outcome,
        workspaces_total=total,
        workspaces_enforcing=enforcing,
        workspaces_observing=observing,
        problems=tuple(problems),
    )


def unresolvable_posture(settings: Settings, *, detail: str) -> PostureReport:
    """The report for "the workspace inventory could not be read".

    Distinct from OBSERVING on purpose. An operator seeing SCOPE_UNRESOLVED
    knows the control's status is unknown; an operator seeing OBSERVING would
    reasonably conclude it is running and simply not denying.
    """
    return PostureReport(
        state=SCOPE_UNRESOLVED,
        declared_posture=settings.workspace_authorization_posture,
        unresolved_scope_outcome=_unresolved_scope_outcome(settings),
        workspaces_total=None,
        workspaces_enforcing=None,
        workspaces_observing=None,
        problems=(f"workspace inventory unreadable: {detail}",),
    )


def assert_startup_posture(report: PostureReport) -> None:
    """Refuse to start a process whose enforcement claim is contradicted.

    Only an ENFORCING claim can fail here, and only on an *observed*
    contradiction. A `SCOPE_UNRESOLVED` report is explicitly not a startup
    failure: the database being down at boot is already reported by readiness,
    and turning it into a crash-loop would make the F11 fix an availability
    regression for deployments that adopted the stricter posture -- which is the
    fastest way to get it turned back off.
    """
    if report.declared_posture != ENFORCING or report.state == SCOPE_UNRESOLVED:
        return
    if report.problems:
        raise PostureViolation(
            "workspace_authorization_posture=ENFORCING is contradicted by the "
            "inspected state: " + "; ".join(report.problems)
        )
