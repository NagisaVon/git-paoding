"""Public library facade: one typed function per CLI verb.

The facade owns repository/session discovery and backend injection.  Frontends
receive only typed Pydantic result objects and never depend on CLI internals.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from git_paoding.core.model import (
    AssignBatchRequest,
    AssignResult,
    AtomState,
    PaodingError,
    PublishResult,
    Session,
    SessionAlreadyExistsError,
    Slice,
    SliceStatus,
    StatusResult,
)
from git_paoding.core.publish import (
    archive_session,
    publish_session,
    reconcile_and_status,
    status_from_session,
)
from git_paoding.core.selectors import assign_batch_selectors, assign_selectors
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


class SessionArchivedError(PaodingError):
    """Raised when a local mutation targets a completed review session."""


def _require_open_session(session: Session) -> None:
    if session.archived:
        raise SessionArchivedError("This review session is archived")


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
        _require_open_session(session)
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


def get_full_status(
    repo: Path,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Return read-only status with complete current Git hunk payloads."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    session = JsonSessionStore(repository).load(branch)
    _session, _replay_atoms, status = reconcile_and_status(repository, session, full=True)
    return status


def assign(
    repo: Path,
    slice_id: str,
    selectors: Sequence[str],
    *,
    canonical_branch: str | None = None,
    force: bool = False,
) -> AssignResult:
    """Assign current atoms with exact-id or broad-selector safety semantics."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        _require_open_session(session)
        target = next((slice_ for slice_ in session.slices if slice_.id == slice_id), None)
        if target is None or target.status is not SliceStatus.ACTIVE:
            raise SliceNotFoundError(f"No active slice exists with id {slice_id!r}")
        session, _replay_atoms, _status = reconcile_and_status(repository, session)
        atoms, result = assign_selectors(
            session.atoms,
            slice_id=slice_id,
            selectors=selectors,
            force=force,
        )
        session = session.model_copy(update={"atoms": list(atoms)})
        store.save(session)
        return result


def assign_batch(
    repo: Path,
    request: AssignBatchRequest,
    *,
    canonical_branch: str | None = None,
) -> AssignResult:
    """Validate and apply one complete batch under one lock and one save."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        _require_open_session(session)
        session, _replay_atoms, _status = reconcile_and_status(repository, session)
        active_ids = {slice_.id for slice_ in session.slices if slice_.status is SliceStatus.ACTIVE}
        atoms, result = assign_batch_selectors(
            session.atoms,
            assignments=request.assignments,
            active_slice_ids=active_ids,
            force=request.force,
        )
        store.save(session.model_copy(update={"atoms": list(atoms)}))
        return result


def remove_slice(
    repo: Path,
    slice_id: str,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Remove a slice locally and return its atoms to unassigned state."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        _require_open_session(session)
        session, _replay_atoms, reconciled_status = reconcile_and_status(repository, session)
        target_index = next(
            (
                index
                for index, slice_ in enumerate(session.slices)
                if slice_.id == slice_id and slice_.status is SliceStatus.ACTIVE
            ),
            None,
        )
        if target_index is None:
            raise SliceNotFoundError(f"No active slice exists with id {slice_id!r}")
        slices = list(session.slices)
        slices[target_index] = slices[target_index].model_copy(
            update={"status": SliceStatus.ARCHIVED}
        )
        atoms = [
            atom.model_copy(update={"owner": None, "state": AtomState.UNASSIGNED})
            if atom.owner == slice_id
            else atom
            for atom in session.atoms
        ]
        session = session.model_copy(
            update={
                "slices": slices,
                "atoms": atoms,
                "focus_slice": None if session.focus_slice == slice_id else session.focus_slice,
            }
        )
        store.save(session)
        return status_from_session(
            session,
            defaulted_atom_ids=tuple(reconciled_status.defaulted_atom_ids),
        )


def rename_slice(
    repo: Path,
    slice_id: str,
    title: str,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Rename an active slice without changing its stable identity or PR mapping."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        _require_open_session(session)
        session, _replay_atoms, reconciled_status = reconcile_and_status(repository, session)
        slices = list(session.slices)
        for index, slice_ in enumerate(slices):
            if slice_.id == slice_id and slice_.status is SliceStatus.ACTIVE:
                slices[index] = slice_.model_copy(update={"title": title})
                break
        else:
            raise SliceNotFoundError(f"No active slice exists with id {slice_id!r}")
        session = session.model_copy(update={"slices": slices})
        store.save(session)
        return status_from_session(
            session,
            defaulted_atom_ids=tuple(reconciled_status.defaulted_atom_ids),
        )


def set_focus(
    repo: Path,
    slice_id: str | None,
    *,
    canonical_branch: str | None = None,
) -> StatusResult:
    """Set or clear the session-global prior for genuinely new atoms."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        session = store.load(branch)
        _require_open_session(session)
        session, _replay_atoms, reconciled_status = reconcile_and_status(repository, session)
        if slice_id is not None and not any(
            slice_.id == slice_id and slice_.status is SliceStatus.ACTIVE
            for slice_ in session.slices
        ):
            raise SliceNotFoundError(f"No active slice exists with id {slice_id!r}")
        session = session.model_copy(update={"focus_slice": slice_id})
        store.save(session)
        return status_from_session(
            session,
            defaulted_atom_ids=tuple(reconciled_status.defaulted_atom_ids),
        )


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


def archive(
    repo: Path,
    *,
    backend: GitHubBackend,
    canonical_branch: str | None = None,
    remote: str = "origin",
) -> StatusResult:
    """Archive every slice PR and generated ref after integration."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    return archive_session(
        repository,
        canonical_branch=branch,
        backend=backend,
        remote=remote,
    )


__all__ = [
    "add_slice",
    "archive",
    "assign",
    "assign_batch",
    "get_full_status",
    "get_status",
    "init_session",
    "publish",
    "remove_slice",
    "rename_slice",
    "set_focus",
]
