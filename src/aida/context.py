from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="unknown")


def get_correlation_id() -> str:
    return correlation_id_var.get()
