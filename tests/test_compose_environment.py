"""The local stack must read `.env`, and must not let it redirect the wiring.

Two failures, one on each side of the same mechanism.

**The one that happened.** `compose.yaml` listed roughly thirty-five variables
per app service and had no `env_file`, so a setting the documentation told an
operator to put in `.env` reached the container only if it happened to be on
that list. `AIDA_DELIVERY_WORKER_ENABLED`,
`AIDA_GOVERNANCE_NOTIFICATIONS_ENABLED`, the Slack and Teams webhook URLs, the
reviewer-agent controls and every task agent's interval were not, so the
features looked broken while the configuration looked correct. Found on
2026-09-12 while trying to produce a single delivery attempt for
`scripts/delivery_history.py`: the flag was set, the container was rebuilt, and
`printenv` in the container knew nothing about it.

**The one this prevents.** The fix relies on Compose precedence: `env_file`
fills gaps and the explicit `environment` block overrides it. That is only safe
while every container-internal address stays in the block, because a
developer's `.env` points the very same names at localhost so host-side scripts
can use them. Drop `AIDA_DATABASE_URL` from the block as a tidy-up and the api
container starts talking to `localhost:5432` -- which inside a container is the
container, so it fails in a way that looks like Postgres being down rather than
like a compose edit.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "compose.yaml"

#: Services that run platform code and therefore read `Settings`.
APP_SERVICES = (
    "migrate",
    "api",
    "seed",
    "metadata-worker",
    "fleet-scheduler",
    "outbox-publisher",
    "graph-projector",
)

#: Names whose value must be the container-internal one, never a host-side
#: `.env` value. Each is a service hostname or a credential for one.
CONTAINER_WIRING = (
    "AIDA_DATABASE_URL",
    "AIDA_REDIS_URL",
    "AIDA_KAFKA_BOOTSTRAP_SERVERS",
    "AIDA_TEMPORAL_ADDRESS",
    "AIDA_NEO4J_URI",
    "AIDA_OBJECT_STORE_ENDPOINT",
    "AIDA_OBJECT_STORE_ACCESS_KEY",
    "AIDA_OBJECT_STORE_SECRET_KEY",
)


@pytest.fixture(scope="module")
def compose() -> dict:
    # `yaml.safe_load` keeps `${VAR:-default}` as the literal string, which is
    # what these assertions want: this file is about what compose *declares*,
    # not about what a particular developer's environment resolves it to.
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _environment(service: dict) -> dict[str, object]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        pairs = (item.split("=", 1) for item in env)
        return {key: value for key, *rest in pairs for value in (rest or [""])}
    return dict(env)


def test_every_app_service_reads_the_env_file(compose: dict) -> None:
    """Otherwise a documented setting silently does nothing in Docker."""
    for name in APP_SERVICES:
        service = compose["services"][name]
        assert service.get("env_file"), f"{name} does not read .env"


def test_the_env_file_is_optional(compose: dict) -> None:
    """A fresh clone has no `.env`, and `docker compose up` must still work --
    a required env_file turns a missing optional file into a hard start
    failure."""
    for name in APP_SERVICES:
        entries = compose["services"][name]["env_file"]
        for entry in entries:
            assert isinstance(entry, dict), (
                f"{name}: use the mapping form with `required: false`, "
                "or a missing .env stops the stack from starting"
            )
            assert entry.get("required") is False, f"{name}: .env must be optional"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_container_wiring_is_never_left_to_the_env_file(
    compose: dict, name: str
) -> None:
    """The precedence guard.

    `env_file` cannot override `environment`, so every one of these staying
    explicit is what keeps a host-side `.env` from redirecting a container at
    localhost. This is the assertion that makes reading `.env` safe.
    """
    env = _environment(compose["services"][name])
    missing = [key for key in CONTAINER_WIRING if key not in env]
    assert not missing, (
        f"{name} leaves {missing} to .env. A developer's .env points those at "
        "localhost for host-side scripts; inside a container localhost is the "
        "container. Keep them in the `environment` block."
    )


def test_no_container_wiring_value_points_at_localhost(compose: dict) -> None:
    """The same mistake made directly rather than by omission."""
    for name in APP_SERVICES:
        env = _environment(compose["services"][name])
        for key in CONTAINER_WIRING:
            value = str(env.get(key, ""))
            assert "localhost" not in value and "127.0.0.1" not in value, (
                f"{name}.{key} points at localhost, which inside a container "
                "is the container itself"
            )


def test_the_feature_flags_an_operator_is_told_to_set_are_reachable(
    compose: dict,
) -> None:
    """A representative sample of what was unreachable, asserted by *mechanism*
    rather than by name.

    Listing every flag here would be a second inventory to keep in step with
    `Settings`, and it would go stale the way the `environment` block did. What
    matters is that the mechanism exists: with `env_file` present, any
    `AIDA_*` name reaches the container without a compose edit, so this checks
    the two services those flags act on rather than enumerating the flags.
    """
    for name in ("api", "fleet-scheduler"):
        service = compose["services"][name]
        assert service.get("env_file"), (
            f"{name} must read .env -- the delivery worker, governance "
            "notifications, the webhook URLs, the reviewer-agent controls and "
            "the task agent intervals are all configured there and none of "
            "them is in the explicit environment block"
        )
