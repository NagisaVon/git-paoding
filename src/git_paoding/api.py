"""Public library facade for the T07 command surface.

The facade owns repository/session discovery and backend injection.  Frontends
receive only typed Pydantic result objects and never depend on CLI internals.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from git_paoding.core.model import (
    AssignResult,
    PaodingError,
    PublishResult,
    Session,
    SessionAlreadyExistsError,
    Slice,
    SliceStatus,
    StatusResult,
)
from git_paoding.core.publish import publish_session, reconcile_and_status
from git_paoding.core.selectors import assign_selectors
from git_paoding.github.backend import GitHubBackend
from git_paoding.gitio.plumbing import rev_parse
from git_paoding.gitio.runner import GitCommandError, run_git
from git_paoding.store.jsonstore import JsonSessionStore
from git_paoding.store.lock import SessionLock


class BranchResolutionError(PaodingError):
    """Raised when no canonical branch can be selected safely."""


class SliceAlreadyExistsError(PaodingError):
    """Raised when adding a duplicate stable slice id."""


class SliceNotFoundError(PaodingError):
    """Raised when an operation names an unknown active slice."""


def _canonical_branch(repo: Path, requested: str | None) -> str:
    if requested is not None:
        if not requested:
            raise BranchResolutionError("canonical_branch must not be empty")
        return requested.removeprefix("refs/heads/")
    try:
        branch = run_git(("symbolic-ref", "--quiet", "--short", "HEAD"), cwd=repo).stdout_text()
    except GitCommandError as error:
        raise BranchResolutionError(
            "Could not infer a canonical branch from detached HEAD; pass canonical_branch "
            "to the library facade or check out the intended integration branch"
        ) from error
    branch = branch.strip()
    if not branch:
        raise BranchResolutionError("Git returned an empty canonical branch name")
    return branch


def init_session(
    repo: Path,
    base: str,
    *,
    backend: GitHubBackend,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Pin ``base`` and initialize a session for the canonical branch."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        if store.exists(branch):
            raise SessionAlreadyExistsError(
                f"A git-paoding session already exists for branch {branch!r}"
            )
        backend.check_ready()
        base_oid = rev_parse(repository, f"{base}^{{commit}}")
        # Verify the canonical branch ref independently of the current checkout.
        rev_parse(repository, f"refs/heads/{branch}^{{commit}}")
        session = Session(
            canonical_branch=branch,
            base_ref=base,
            base_oid=base_oid,
        )
        session, _replay_atoms, status = reconcile_and_status(repository, session)
        store.save(session)
        return status


def add_slice(
    repo: Path,
    slice_id: str,
    title: str,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Add one active stable slice identity and return refreshed status."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    new_slice = Slice(id=slice_id, title=title)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        if any(slice_.id == slice_id for slice_ in session.slices):
            raise SliceAlreadyExistsError(f"Slice {slice_id!r} already exists")
        session = session.model_copy(update={"slices": [*session.slices, new_slice]})
        session, _replay_atoms, status = reconcile_and_status(repository, session)
        store.save(session)
        return status


def get_status(
    repo: Path,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Reconcile against the live canonical tip and report status.

    Fully read-only: no refs, no GitHub calls, and no session writes.
    Reconciliation is deterministic, so a following ``assign`` re-derives the
    same atom ids without any persisted cache.
    """

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    session = store.load(branch)
    _session, _replay_atoms, status = reconcile_and_status(repository, session)
    return status


def assign(
    repo: Path,
    slice_id: str,
    selectors: Sequence[str],
    *,
    canonical_branch: str | None = None,
) -> AssignResult:
    """Assign current atoms by id or exact path and echo every selected atom."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        target = next((slice_ for slice_ in session.slices if slice_.id == slice_id), None)
        if target is None or target.status is not SliceStatus.ACTIVE:
            raise SliceNotFoundError(f"No active slice exists with id {slice_id!r}")
        session, _replay_atoms, _status = reconcile_and_status(repository, session)
        atoms, result = assign_selectors(
            session.atoms,
            slice_id=slice_id,
            selectors=selectors,
        )
        session = session.model_copy(update={"atoms": list(atoms)})
        store.save(session)
        return result


def publish(
    repo: Path,
    *,
    backend: GitHubBackend,
    canonical_branch: str | None = None,
    remote: str = "origin",
) -> PublishResult:
    """Run the idempotent projection/ref/PR publication pipeline."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    return publish_session(
        repository,
        canonical_branch=branch,
        backend=backend,
        remote=remote,
    )


__all__ = ["add_slice", "assign", "get_status", "init_session", "publish"]
