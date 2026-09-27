"""Every deployment path must migrate the same way (R11-AUD13).

`compose.yaml`'s `migrate` service runs `alembic upgrade heads`, deliberately plural: parallel
work on this repository has produced several Alembic heads at once, and `upgrade head` fails
as soon as there is more than one. `infra/k8s/base/migration-job.yaml` ran the singular form,
so the two agreed only while the tree happened to have one head, and the Kubernetes Job
would have been the first thing to fail on the day it did not.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "compose.yaml"
MIGRATION_JOB = REPO_ROOT / "infra" / "k8s" / "base" / "migration-job.yaml"

EXPECTED = ["alembic", "upgrade", "heads"]


def _job_command() -> list[str]:
    job: dict[str, Any] = yaml.safe_load(MIGRATION_JOB.read_text(encoding="utf-8"))
    containers = job["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "the migration Job is expected to run one container"
    command: list[str] = containers[0]["command"]
    return command


def _compose_command() -> list[str]:
    compose: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    command: list[str] = compose["services"]["migrate"]["command"]
    return command


def test_compose_migrates_every_head() -> None:
    assert _compose_command() == EXPECTED


def test_the_kubernetes_migration_job_migrates_every_head() -> None:
    assert _job_command() == EXPECTED


def test_both_deployment_paths_run_the_same_migration_command() -> None:
    assert _job_command() == _compose_command()
