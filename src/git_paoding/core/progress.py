"""Safe, frontend-neutral progress events for publication."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from time import perf_counter


class PublishPhase(str, Enum):
    """Stable phases in the publish pipeline."""

    RECONCILE = "reconcile"
    VALIDATE_GITHUB = "validate-github"
    LOAD_CONTEXT = "load-context"
    BUILD_PROJECTION = "build-projection"
    SYNC_REFS = "sync-refs"
    SLICE_PR = "slice-pr"
    INTEGRATION_INDEX = "integration-index"
    PERSIST = "persist"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One pre-rendered progress update containing only safe display text."""

    phase: PublishPhase
    message: str
    index: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True, slots=True)
class _ActiveProgress:
    callback: ProgressCallback
    event: ProgressEvent


_active_progress: ContextVar[_ActiveProgress | None] = ContextVar(
    "git_paoding_active_progress",
    default=None,
)


@contextmanager
def publish_phase(
    callback: ProgressCallback | None,
    event: ProgressEvent,
) -> Iterator[None]:
    """Report and time a phase while exposing it to subprocess boundaries."""

    if callback is not None:
        callback(event)
        token = _active_progress.set(_ActiveProgress(callback=callback, event=event))
    else:
        token = None
    started = perf_counter()
    try:
        yield
    finally:
        from git_paoding.gitio.trace import record_phase

        record_phase(event.phase, perf_counter() - started)
        if token is not None:
            _active_progress.reset(token)


def report_network_process() -> None:
    """Repeat the current safe event immediately before a network process."""

    active = _active_progress.get()
    if active is not None:
        active.callback(active.event)
