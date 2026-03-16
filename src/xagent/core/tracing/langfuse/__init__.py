"""Shared Langfuse tracing utilities."""

from .client import flush_langfuse, initialize_langfuse, reset_langfuse_client
from .handler import LangfuseTraceHandler


def create_langfuse_trace_handler(
    *,
    task_id: str,
    user_id: int | None = None,
    trace_name: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> LangfuseTraceHandler | None:
    """Create a Langfuse trace handler when tracing is configured."""
    from .client import get_langfuse_client

    if get_langfuse_client() is None:
        return None

    return LangfuseTraceHandler(
        task_id=task_id,
        user_id=user_id,
        trace_name=trace_name,
        session_id=session_id,
        tags=tags,
        metadata=metadata,
    )


__all__ = [
    "LangfuseTraceHandler",
    "create_langfuse_trace_handler",
    "flush_langfuse",
    "initialize_langfuse",
    "reset_langfuse_client",
]
