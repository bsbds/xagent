"""Web tracer factory helpers."""

from __future__ import annotations

from typing import Optional

from ..core.agent.trace import Tracer
from ..core.tracing.langfuse import create_langfuse_trace_handler
from .api.trace_handlers import DatabaseTraceHandler
from .api.ws_trace_handlers import WebSocketTraceHandler
from .models.user import User


def create_task_tracer(task_id: int, user: Optional[User] = None) -> Tracer:
    """Build the standard task tracer stack for web execution."""
    tracer = Tracer()

    from ..core.agent.trace import ConsoleTraceHandler

    tracer.add_handler(ConsoleTraceHandler())
    tracer.add_handler(DatabaseTraceHandler(task_id))
    tracer.add_handler(WebSocketTraceHandler(task_id))

    langfuse_handler = create_langfuse_trace_handler(
        task_id=str(task_id),
        user_id=int(user.id) if user and user.id is not None else None,
        trace_name=f"xagent-web-task-{task_id}",
        session_id=f"task:{task_id}",
        tags=["xagent", "web", "task"],
        metadata={"task_id": task_id, "is_preview": False},
    )
    if langfuse_handler is not None:
        tracer.add_handler(langfuse_handler)

    return tracer
