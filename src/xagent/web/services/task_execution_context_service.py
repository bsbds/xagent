"""Helpers for deriving reusable cross-round execution context from persisted traces."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...skills.utils import create_skill_manager
from ..models.task import TraceEvent


async def load_task_execution_recovery_state(
    db: Session,
    task_id: int,
    *,
    max_tool_events: int = 8,
) -> Dict[str, Any]:
    """Load reusable execution recovery state for a task."""
    return {
        "messages": load_task_execution_context_messages(
            db, task_id, max_tool_events=max_tool_events
        ),
        "skill_context": await load_task_recovered_skill_context(db, task_id),
    }


def load_task_execution_context_messages(
    db: Session,
    task_id: int,
    *,
    max_tool_events: int = 8,
) -> List[Dict[str, str]]:
    """Load reusable prior execution context for a task as planner-visible messages."""
    trace_events = (
        db.query(TraceEvent)
        .filter(
            TraceEvent.task_id == task_id,
            TraceEvent.build_id.is_(None),
            TraceEvent.event_type == "tool_execution_end",
        )
        .order_by(TraceEvent.timestamp.desc(), TraceEvent.id.desc())
        .limit(max_tool_events)
        .all()
    )

    tool_summaries: List[str] = []
    seen_summaries: set[str] = set()

    for trace_event in trace_events:
        data: Dict[str, Any] = (
            trace_event.data if isinstance(trace_event.data, dict) else {}
        )
        summary = summarize_tool_event(data)
        if not summary or summary in seen_summaries:
            continue
        seen_summaries.add(summary)
        tool_summaries.append(summary)

    if not tool_summaries:
        return []

    tool_summaries.reverse()
    content = (
        "=== Previous Execution Context ===\n"
        "These are reusable results discovered in earlier rounds of this same task.\n"
        "Use them when handling follow-up requests, and only rerun tools if fresher data is needed.\n"
        + "\n".join(tool_summaries)
    )
    return [{"role": "system", "content": content}]


async def load_task_recovered_skill_context(db: Session, task_id: int) -> Optional[str]:
    """Load the latest selected skill context for a task, if any."""
    trace_event = (
        db.query(TraceEvent)
        .filter(
            TraceEvent.task_id == task_id,
            TraceEvent.build_id.is_(None),
            TraceEvent.event_type == "skill_select_end",
        )
        .order_by(TraceEvent.timestamp.desc(), TraceEvent.id.desc())
        .first()
    )
    if trace_event is None or not isinstance(trace_event.data, dict):
        return None

    selected = bool(trace_event.data.get("selected", False))
    skill_name = str(trace_event.data.get("skill_name") or "").strip()
    if not selected or not skill_name:
        return None

    return await _load_skill_context_by_name(skill_name)


def summarize_tool_event(data: Dict[str, Any]) -> Optional[str]:
    """Summarize a persisted tool execution event into reusable context text."""
    tool_name = str(data.get("tool_name") or "").strip()
    if not tool_name:
        return None

    if not bool(data.get("success", True)):
        return None

    result = data.get("result")
    summary = _summarize_generic_result(result)
    if not summary:
        return None
    return f"- Tool {tool_name} previously returned: {summary}"


def _summarize_generic_result(result: Any, max_length: int = 240) -> Optional[str]:
    if result is None:
        return None
    if isinstance(result, str):
        preview = result.strip()
    else:
        try:
            preview = json.dumps(result, ensure_ascii=False)
        except Exception:
            preview = str(result).strip()

    if not preview:
        return None
    if len(preview) > max_length:
        return preview[: max_length - 3] + "..."
    return preview


async def _load_skill_context_by_name(skill_name: str) -> Optional[str]:
    skill_manager = create_skill_manager()
    skill = await skill_manager.get_skill(skill_name)
    if not skill:
        return None
    return _build_skill_context(skill)


def _build_skill_context(skill: Dict[str, Any]) -> str:
    content = str(skill.get("content", "")).strip()
    return f"## Available Skill: {skill['name']}\n\n{content}"
