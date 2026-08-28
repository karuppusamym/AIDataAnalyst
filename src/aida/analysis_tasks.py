from typing import Any

TASK_TYPE_DISCOVER_DATASOURCE = "DISCOVER_DATASOURCE"
TASK_TYPE_PLAN_PROFILE_TASKS = "PLAN_PROFILE_TASKS"
TASK_TYPE_PROFILE_TABLE = "PROFILE_TABLE"
TASK_TYPE_FINALIZE_PROFILE_TASKS = "FINALIZE_PROFILE_TASKS"
TASK_TYPE_PROFILE_DATASOURCE = "PROFILE_DATASOURCE"

TASK_TYPE_LABELS = {
    TASK_TYPE_DISCOVER_DATASOURCE: "Discover datasource",
    TASK_TYPE_PLAN_PROFILE_TASKS: "Plan profiling tasks",
    TASK_TYPE_PROFILE_TABLE: "Profile table",
    TASK_TYPE_FINALIZE_PROFILE_TASKS: "Finalize profiling",
    TASK_TYPE_PROFILE_DATASOURCE: "Profile datasource",
}


def task_display_name(task_type: str, details: dict[str, Any] | None = None) -> str:
    details = details or {}
    if task_type == TASK_TYPE_PROFILE_TABLE:
        schema_name = str(details.get("schema_name") or "").strip()
        table_name = str(details.get("table_name") or "").strip()
        if schema_name and table_name:
            return f"Profile {schema_name}.{table_name}"
        if table_name:
            return f"Profile {table_name}"
    return TASK_TYPE_LABELS.get(task_type, task_type.replace("_", " ").title())
