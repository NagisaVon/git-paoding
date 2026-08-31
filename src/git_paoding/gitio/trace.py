"""Context-local aggregate subprocess accounting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from git_paoding.core.progress import PublishPhase


class OpCategory(str, Enum):
    """Stable categories for publish subprocess accounting."""

    GIT_LOCAL = "git-local"
    GIT_REMOTE = "git-remote"
    GH_READ = "gh-read"
    GH_WRITE = "gh-write"


@dataclass(slots=True)
class SubprocessTrace:
    """Aggregate process counts, durations, and publish phase durations."""

    counts: Counter[OpCategory] = field(default_factory=Counter)
    durations: dict[OpCategory, float] = field(default_factory=dict)
    phase_durations: dict[str, float] = field(default_factory=dict)


_collector: ContextVar[SubprocessTrace | None] = ContextVar(
    "git_paoding_subprocess_trace",
    default=None,
)


@contextmanager
def collecting() -> Iterator[SubprocessTrace]:
    """Collect subprocess aggregates in the current context."""

    trace = SubprocessTrace()
    token = _collector.set(trace)
    try:
        yield trace
    finally:
        _collector.reset(token)


def record(category: OpCategory, seconds: float) -> None:
    """Record one completed or timed-out subprocess when collection is active."""

    trace = _collector.get()
    if trace is None:
        return
    trace.counts[category] += 1
    trace.durations[category] = trace.durations.get(category, 0.0) + max(seconds, 0.0)


def record_phase(phase: PublishPhase, seconds: float) -> None:
    """Record elapsed time for one publish phase when collection is active."""

    trace = _collector.get()
    if trace is None:
        return
    key = phase.value
    trace.phase_durations[key] = trace.phase_durations.get(key, 0.0) + max(seconds, 0.0)
