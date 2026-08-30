"""Create persisted tables for completed control-plane capabilities.

Revision ID: e8f1a2b3c4d5
Revises: c4d8e6f0a1b3
Create Date: 2026-08-30

The corresponding APIs and ORM models shipped before their Alembic DDL.  A clean
``alembic upgrade head`` therefore exposed the routes but returned 500 for every
database-backed operation.  The SQL below is deliberately frozen in this revision;
it does not import live ORM metadata, so future model changes cannot rewrite history.
"""

# Frozen SQL mirrors the PostgreSQL DDL and is intentionally kept readable as SQL.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "e8f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "c4d8e6f0a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE abac_policy (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      policy_key VARCHAR(100) NOT NULL, version INTEGER NOT NULL, name VARCHAR(200) NOT NULL,
      description TEXT NOT NULL, effect VARCHAR(10) NOT NULL, subject_conditions JSON NOT NULL,
      resource_conditions JSON NOT NULL, environment_conditions JSON NOT NULL, priority INTEGER NOT NULL,
      status VARCHAR(30) NOT NULL, created_by VARCHAR(255) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      CONSTRAINT uq_abac_policy_organization_id UNIQUE (organization_id, policy_key, version)
    )
    """,
    """
    CREATE TABLE abac_decision (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      principal_id VARCHAR(255) NOT NULL, principal_type VARCHAR(30) NOT NULL, decision VARCHAR(10) NOT NULL,
      resource_type VARCHAR(100) NOT NULL, resource_id VARCHAR(255), subject_attributes JSON NOT NULL,
      resource_attributes JSON NOT NULL, environment_attributes JSON NOT NULL,
      contributing_policy_ids JSON NOT NULL, reasons JSON NOT NULL, evaluation_time_ms FLOAT NOT NULL,
      policy_version VARCHAR(100) NOT NULL, correlation_id VARCHAR(100) NOT NULL,
      evaluated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE ai_decision_record (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      run_id UUID NOT NULL, decision_type VARCHAR(30) NOT NULL, source_node VARCHAR(500) NOT NULL,
      target_node VARCHAR(500) NOT NULL, reason VARCHAR(2000) NOT NULL, evidence JSON NOT NULL,
      control_version VARCHAR(100), decided_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE audit_archive_record (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      archive_id VARCHAR(200) NOT NULL UNIQUE, event_count INTEGER NOT NULL,
      event_range_start TIMESTAMPTZ NOT NULL, event_range_end TIMESTAMPTZ NOT NULL,
      checksum VARCHAR(64) NOT NULL, storage_backend VARCHAR(30) NOT NULL,
      retention_until TIMESTAMPTZ NOT NULL, legal_hold BOOLEAN NOT NULL, created_by VARCHAR(255) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE compliance_pack (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      name VARCHAR(200) NOT NULL, framework VARCHAR(50) NOT NULL, period_start TIMESTAMPTZ NOT NULL,
      period_end TIMESTAMPTZ NOT NULL, sections JSON NOT NULL, status VARCHAR(30) NOT NULL,
      checksum VARCHAR(64) NOT NULL, generated_by VARCHAR(255) NOT NULL, generated_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE slo_definition (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      slo_key VARCHAR(100) NOT NULL, name VARCHAR(200) NOT NULL, target FLOAT NOT NULL,
      window_days INTEGER NOT NULL, threshold FLOAT NOT NULL, status VARCHAR(30) NOT NULL,
      created_by VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      CONSTRAINT uq_slo_definition_organization_id UNIQUE (organization_id, slo_key)
    )
    """,
    """
    CREATE TABLE slo_measurement (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      slo_id UUID NOT NULL REFERENCES slo_definition(id) ON DELETE CASCADE, value FLOAT NOT NULL,
      budget_remaining FLOAT NOT NULL, measured_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE studio_change_set (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      name VARCHAR(200) NOT NULL, author VARCHAR(255) NOT NULL, status VARCHAR(30) NOT NULL,
      base_version_hash VARCHAR(64) NOT NULL, conflict_status VARCHAR(30) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE studio_change_item (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      change_set_id UUID NOT NULL REFERENCES studio_change_set(id) ON DELETE CASCADE,
      object_type VARCHAR(50) NOT NULL, object_id VARCHAR(100) NOT NULL, operation VARCHAR(30) NOT NULL,
      before_snapshot JSON, after_snapshot JSON, diff JSON, test_status VARCHAR(30) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE studio_test_run (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      change_set_id UUID NOT NULL REFERENCES studio_change_set(id) ON DELETE CASCADE,
      started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, passed BOOLEAN NOT NULL,
      evidence JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE tool_plan (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      name VARCHAR(200) NOT NULL, budget JSON NOT NULL, status VARCHAR(30) NOT NULL,
      created_by VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE tool_plan_execution (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      plan_id UUID NOT NULL REFERENCES tool_plan(id) ON DELETE CASCADE, started_at TIMESTAMPTZ NOT NULL,
      completed_at TIMESTAMPTZ, budget_consumed JSON NOT NULL, status VARCHAR(30) NOT NULL,
      executed_by VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE tool_plan_step (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      plan_id UUID NOT NULL REFERENCES tool_plan(id) ON DELETE CASCADE, sequence INTEGER NOT NULL,
      tool_id VARCHAR(255) NOT NULL, tool_version VARCHAR(50) NOT NULL, parameters JSON NOT NULL,
      dependencies JSON NOT NULL, timeout_seconds INTEGER NOT NULL, expected_cost FLOAT NOT NULL,
      status VARCHAR(30) NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
      evidence JSON NOT NULL, error_message VARCHAR(1000), created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL, CONSTRAINT uq_tool_plan_step_sequence UNIQUE (plan_id, sequence)
    )
    """,
    """
    CREATE TABLE contract_violation (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      contract_id UUID NOT NULL REFERENCES data_contract_version(id) ON DELETE CASCADE,
      violation_type VARCHAR(30) NOT NULL, severity VARCHAR(20) NOT NULL, evidence JSON NOT NULL,
      detected_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ, resolved_by VARCHAR(255),
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE contract_sla_record (
      id UUID PRIMARY KEY, organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
      contract_id UUID NOT NULL REFERENCES data_contract_version(id) ON DELETE CASCADE,
      period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL, uptime_percent FLOAT NOT NULL,
      violations_count INTEGER NOT NULL, breach_minutes INTEGER NOT NULL,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      CONSTRAINT uq_contract_sla_period UNIQUE (contract_id, period_start)
    )
    """,
)


_INDEXES: tuple[str, ...] = (
    "CREATE INDEX ix_abac_policy_organization_id ON abac_policy (organization_id)",
    "CREATE INDEX ix_abac_policy_org_status ON abac_policy (organization_id, status)",
    "CREATE INDEX ix_abac_decision_principal ON abac_decision (principal_id, evaluated_at)",
    "CREATE INDEX ix_abac_decision_org_created ON abac_decision (organization_id, evaluated_at)",
    "CREATE INDEX ix_abac_decision_correlation_id ON abac_decision (correlation_id)",
    "CREATE INDEX ix_abac_decision_organization_id ON abac_decision (organization_id)",
    "CREATE INDEX ix_ai_decision_asset ON ai_decision_record (target_node, decided_at)",
    "CREATE INDEX ix_ai_decision_record_organization_id ON ai_decision_record (organization_id)",
    "CREATE INDEX ix_ai_decision_org_created ON ai_decision_record (organization_id, decided_at)",
    "CREATE INDEX ix_ai_decision_record_run_id ON ai_decision_record (run_id)",
    "CREATE INDEX ix_ai_decision_refusals ON ai_decision_record (organization_id, decision_type) WHERE decision_type = 'REFUSAL'",
    "CREATE INDEX ix_ai_decision_run ON ai_decision_record (run_id, decision_type)",
    "CREATE INDEX ix_audit_archive_record_organization_id ON audit_archive_record (organization_id)",
    "CREATE INDEX ix_audit_archive_org_created ON audit_archive_record (organization_id, created_at)",
    "CREATE INDEX ix_compliance_pack_org_framework ON compliance_pack (organization_id, framework)",
    "CREATE INDEX ix_compliance_pack_organization_id ON compliance_pack (organization_id)",
    "CREATE INDEX ix_compliance_pack_org_created ON compliance_pack (organization_id, created_at)",
    "CREATE INDEX ix_slo_definition_org_status ON slo_definition (organization_id, status)",
    "CREATE INDEX ix_slo_definition_organization_id ON slo_definition (organization_id)",
    "CREATE INDEX ix_slo_measurement_slo_id ON slo_measurement (slo_id)",
    "CREATE INDEX ix_slo_measurement_slo_time ON slo_measurement (slo_id, measured_at)",
    "CREATE INDEX ix_slo_measurement_organization_id ON slo_measurement (organization_id)",
    "CREATE INDEX ix_studio_change_set_org_status ON studio_change_set (organization_id, status)",
    "CREATE INDEX ix_studio_change_set_organization_id ON studio_change_set (organization_id)",
    "CREATE INDEX ix_studio_change_item_change_set_id ON studio_change_item (change_set_id)",
    "CREATE INDEX ix_studio_change_item_organization_id ON studio_change_item (organization_id)",
    "CREATE INDEX ix_studio_change_item_change_set ON studio_change_item (change_set_id)",
    "CREATE INDEX ix_studio_test_run_organization_id ON studio_test_run (organization_id)",
    "CREATE INDEX ix_studio_test_run_change_set ON studio_test_run (change_set_id)",
    "CREATE INDEX ix_studio_test_run_change_set_id ON studio_test_run (change_set_id)",
    "CREATE INDEX ix_tool_plan_organization_id ON tool_plan (organization_id)",
    "CREATE INDEX ix_tool_plan_org_status ON tool_plan (organization_id, status)",
    "CREATE INDEX ix_tool_plan_execution_plan_id ON tool_plan_execution (plan_id)",
    "CREATE INDEX ix_tool_plan_execution_org_created ON tool_plan_execution (organization_id, created_at)",
    "CREATE INDEX ix_tool_plan_execution_plan ON tool_plan_execution (plan_id)",
    "CREATE INDEX ix_tool_plan_execution_organization_id ON tool_plan_execution (organization_id)",
    "CREATE INDEX ix_tool_plan_step_plan ON tool_plan_step (plan_id)",
    "CREATE INDEX ix_tool_plan_step_organization_id ON tool_plan_step (organization_id)",
    "CREATE INDEX ix_tool_plan_step_plan_id ON tool_plan_step (plan_id)",
    "CREATE INDEX ix_contract_violation_organization_id ON contract_violation (organization_id)",
    "CREATE INDEX ix_contract_violation_org_contract ON contract_violation (organization_id, contract_id)",
    "CREATE INDEX ix_contract_violation_contract_id ON contract_violation (contract_id)",
    "CREATE INDEX ix_contract_violation_org_type ON contract_violation (organization_id, violation_type)",
    "CREATE INDEX ix_contract_violation_detected ON contract_violation (organization_id, detected_at)",
    "CREATE INDEX ix_contract_sla_org_contract ON contract_sla_record (organization_id, contract_id)",
    "CREATE INDEX ix_contract_sla_record_organization_id ON contract_sla_record (organization_id)",
    "CREATE INDEX ix_contract_sla_record_contract_id ON contract_sla_record (contract_id)",
)


_DROP_ORDER: tuple[str, ...] = (
    "contract_sla_record", "contract_violation", "tool_plan_step", "tool_plan_execution",
    "tool_plan", "studio_test_run", "studio_change_item", "studio_change_set", "slo_measurement",
    "slo_definition", "compliance_pack", "audit_archive_record", "ai_decision_record",
    "abac_decision", "abac_policy",
)


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for table_name in _DROP_ORDER:
        op.drop_table(table_name)
