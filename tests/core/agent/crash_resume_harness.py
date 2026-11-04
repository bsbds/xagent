"""Stop a pattern run dead at a chosen checkpoint, to get the checkpoint a real
crash would leave behind and resume from it."""

from __future__ import annotations

from typing import Any

from xagent.core.agent import PatternRuntime


class Crash(BaseException):
    """A process death. A ``BaseException`` so that no ``except Exception`` in a
    pattern can absorb it and carry on as if the run had survived."""


class CrashAtCheckpoint(PatternRuntime):
    """Raises :class:`Crash` right after the ``occurrence``-th checkpoint labelled
    ``label`` was recorded, so ``last_checkpoint`` is exactly what a run killed
    at that point leaves in storage. Labels are the ones the runtime receives,
    i.e. with any ``dag_`` / ``auto_child_`` prefix."""

    def __init__(self, label: str, *, occurrence: int = 1) -> None:
        super().__init__()
        self.crash_label = label
        self.crash_occurrence = occurrence
        self._seen = 0

    async def checkpoint(self, label: str, **kwargs: Any) -> dict[str, Any]:
        payload = await super().checkpoint(label, **kwargs)
        if label == self.crash_label:
            self._seen += 1
            if self._seen == self.crash_occurrence:
                raise Crash(label)
        return payload
