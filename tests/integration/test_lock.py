"""Integration tests for fail-fast advisory session locking."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta

import pytest

from conftest import ScratchRepoFactory
from git_paoding.core.model import (
    ConcurrentSessionAccessError,
    Session,
    StaleSessionLockError,
)
from git_paoding.store.jsonstore import JsonSessionStore
from git_paoding.store.lock import SessionLock


@pytest.mark.integration
def test_second_mutating_access_fails_fast(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({}, {})
    first = SessionLock(repo.path, "feature/locked")
    second = SessionLock(repo.path, "feature/locked")

    with first:
        started = time.monotonic()
        with pytest.raises(ConcurrentSessionAccessError, match="concurrent writes fail fast"):
            second.acquire()
        assert time.monotonic() - started < 0.5


@pytest.mark.integration
def test_read_only_store_operation_ignores_mutating_lock(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({}, {})
    branch = "feature/read-only"
    store = JsonSessionStore(repo.path)
    session = Session(canonical_branch=branch, base_oid=repo.base_oid)
    store.save(session)

    with SessionLock(repo.path, branch):
        started = time.monotonic()
        assert store.load(branch) == session
        assert store.exists(branch)
        assert time.monotonic() - started < 0.5


@pytest.mark.integration
def test_stale_lock_uses_dead_pid_and_age_and_gives_override_guidance(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({}, {})
    branch = "feature/stale"
    lock = SessionLock(repo.path, branch, stale_after=timedelta(minutes=5))
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": 99_999_999, "created_at": time.time() - 3600}),
        encoding="utf-8",
    )

    with pytest.raises(StaleSessionLockError, match=r"pid=.*not running.*override_stale=True"):
        lock.acquire()

    with SessionLock(
        repo.path,
        branch,
        stale_after=timedelta(minutes=5),
        override_stale=True,
    ):
        assert lock.path.exists()
    assert not lock.path.exists()


@pytest.mark.integration
def test_live_pid_is_not_stale_even_when_old(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({}, {})
    lock = SessionLock(repo.path, "feature/live", stale_after=timedelta(seconds=1))
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": os.getpid(), "created_at": time.time() - 3600}),
        encoding="utf-8",
    )

    with pytest.raises(ConcurrentSessionAccessError, match=f"pid={os.getpid()}"):
        lock.acquire()


@pytest.mark.integration
def test_recent_dead_pid_is_not_stale_until_age_threshold(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({}, {})
    lock = SessionLock(repo.path, "feature/recent", stale_after=timedelta(hours=1))
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": 99_999_999, "created_at": time.time()}),
        encoding="utf-8",
    )

    with pytest.raises(ConcurrentSessionAccessError, match="concurrent writes fail fast"):
        lock.acquire()
