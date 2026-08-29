"""Fail-fast advisory locking for mutating session operations."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from git_paoding.core.model import (
    ConcurrentSessionAccessError,
    SessionLockError,
    StaleSessionLockError,
)
from git_paoding.store.jsonstore import branch_key, paoding_dir

DEFAULT_STALE_AFTER = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class LockOwner:
    """Process identity persisted in a lock file."""

    pid: int
    created_at: float


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionLock:
    """Exclusive advisory lock scoped to one canonical branch."""

    def __init__(
        self,
        repo: Path,
        canonical_branch: str,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        override_stale: bool = False,
    ) -> None:
        if stale_after.total_seconds() < 0:
            raise ValueError("stale_after must not be negative")
        self.repo = repo.resolve()
        self.canonical_branch = canonical_branch
        self.stale_after = stale_after
        self.override_stale = override_stale
        self.path = paoding_dir(self.repo) / "locks" / f"{branch_key(canonical_branch)}.lock"
        self._acquired = False

    def acquire(self) -> SessionLock:
        """Acquire immediately or fail without waiting."""

        if self._acquired:
            raise SessionLockError(f"Session lock is already held by this object: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                owner = self._read_owner()
                age = max(0.0, time.time() - owner.created_at)
                alive = _pid_is_alive(owner.pid)
                stale = not alive and age >= self.stale_after.total_seconds()
                if not stale:
                    raise ConcurrentSessionAccessError(
                        f"Another mutating git-paoding process holds {self.path} "
                        f"(pid={owner.pid}, age={age:.1f}s); concurrent writes fail fast."
                    ) from None
                if not self.override_stale:
                    raise StaleSessionLockError(
                        f"Stale git-paoding session lock detected at {self.path} "
                        f"(pid={owner.pid} is not running, age={age:.1f}s). "
                        "After verifying no mutating command is active, retry with "
                        "override_stale=True to remove the stale lock."
                    ) from None
                self._remove_stale(owner)
                continue

            owner = LockOwner(pid=os.getpid(), created_at=time.time())
            payload = json.dumps({"pid": owner.pid, "created_at": owner.created_at}) + "\n"
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
            self._acquired = True
            return self

    def release(self) -> None:
        """Release a lock owned by this object."""

        if not self._acquired:
            return
        try:
            current = self._read_owner()
            if current.pid != os.getpid():
                raise SessionLockError(
                    f"Refusing to release session lock {self.path}: ownership changed "
                    f"from pid {os.getpid()} to pid {current.pid}"
                )
            self.path.unlink()
        except FileNotFoundError as error:
            raise SessionLockError(
                f"Session lock disappeared before release: {self.path}"
            ) from error
        finally:
            self._acquired = False

    def _read_owner(self) -> LockOwner:
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("lock payload is not an object")
            pid = payload["pid"]
            created_at = payload["created_at"]
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise TypeError("pid is not an integer")
            if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
                raise TypeError("created_at is not a number")
            return LockOwner(pid=pid, created_at=float(created_at))
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise StaleSessionLockError(
                f"Cannot validate existing session lock {self.path}: {error}. "
                "After verifying no mutating command is active, remove the lock manually "
                "or retry with override_stale=True."
            ) from error

    def _remove_stale(self, expected: LockOwner) -> None:
        current = self._read_owner()
        if current != expected:
            raise ConcurrentSessionAccessError(
                f"Session lock {self.path} changed while checking staleness; retry the operation."
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            return

    def __enter__(self) -> SessionLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
