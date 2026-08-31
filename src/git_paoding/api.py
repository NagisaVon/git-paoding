"""Public library facade: one typed function per CLI verb.

The facade owns repository/session discovery and backend injection.  Frontends
receive only typed Pydantic result objects and never depend on CLI internals.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path

from git_paoding.core.model import (
    AssignBatchRequest,
    AssignResult,
    AtomState,
    PaodingError,
    PRState,
    PublishResult,
    PullRequestTarget,
    ReplaceResult,
    Session,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    Slice,
    SliceStatus,
    SourcePullRequest,
    StatusResult,
)
from git_paoding.core.progress import ProgressCallback
from git_paoding.core.publish import (
    archive_session,
    publish_session,
    reconcile_and_status,
    status_from_session,
)
from git_paoding.core.selectors import assign_batch_selectors, assign_selectors
from git_paoding.github.backend import GitHubBackend
from git_paoding.gitio import plumbing
from git_paoding.gitio.plumbing import rev_parse
from git_paoding.gitio.runner import GitCommandError, run_git
from git_paoding.store.jsonstore import JsonSessionStore, branch_key
from git_paoding.store.lock import SessionLock


class BranchResolutionError(PaodingError):
    """Raised when no canonical branch can be selected safely."""


class InvalidBaseRefError(PaodingError):
    """Raised when local initialization is given a non-branch base ref."""


class PullRequestInitializationError(PaodingError):
    """Raised when pull-request metadata disagrees with the local repository."""


class SliceAlreadyExistsError(PaodingError):
    """Raised when adding a duplicate stable slice id."""


class SliceNotFoundError(PaodingError):
    """Raised when an operation names an unknown active slice."""


class SessionArchivedError(PaodingError):
    """Raised when a local mutation targets a completed review session."""


class SessionReplacementError(PaodingError):
    """Raised when publication evidence makes session replacement unsafe."""


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


def _validated_base_branch(repo: Path, base: str) -> None:
    """Require a local or remote-tracking branch for local initialization."""

    try:
        symbolic_name = (
            run_git(
                (
                    "rev-parse",
                    "--symbolic-full-name",
                    "--verify",
                    "--end-of-options",
                    base,
                ),
                cwd=repo,
            )
            .stdout_text()
            .strip()
        )
    except GitCommandError as error:
        raise InvalidBaseRefError(
            "The base must be a branch or remote-tracking branch; use `init --pr` "
            "when an integration PR exists."
        ) from error
    if not symbolic_name.startswith(("refs/heads/", "refs/remotes/")):
        raise InvalidBaseRefError(
            "The base must be a branch or remote-tracking branch; use `init --pr` "
            "when an integration PR exists."
        )


def init_session(
    repo: Path,
    base: str,
    *,
    backend: GitHubBackend | None = None,
    canonical_branch: str | None = None,
    slice_pr_prefix: str = "slice",
) -> StatusResult:
    """Pin ``base`` and initialize a session for the canonical branch."""

    if backend is not None:
        warnings.warn(
            "The backend argument to init_session() is deprecated and ignored",
            DeprecationWarning,
            stacklevel=2,
        )
    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    session = _build_local_session(
        repository,
        base=base,
        canonical_branch=branch,
        slice_pr_prefix=slice_pr_prefix,
    )
    return _create_session(repository, session)


def _build_local_session(
    repo: Path,
    *,
    base: str,
    canonical_branch: str,
    slice_pr_prefix: str,
) -> Session:
    """Validate local initialization inputs and return an unpersisted session."""

    _validated_base_branch(repo, base)
    base_oid = rev_parse(repo, f"{base}^{{commit}}")
    # Verify the canonical branch ref independently of the current checkout.
    rev_parse(repo, f"refs/heads/{canonical_branch}^{{commit}}")
    return Session(
        canonical_branch=canonical_branch,
        base_ref=base,
        base_oid=base_oid,
        slice_pr_prefix=slice_pr_prefix,
    )


def _create_session(repo: Path, session: Session) -> StatusResult:
    """Create and reconcile a fully validated session under its branch lock."""

    store = JsonSessionStore(repo)
    with SessionLock(repo, session.canonical_branch):
        if store.exists(session.canonical_branch):
            raise SessionAlreadyExistsError(
                f"A git-paoding session already exists for branch {session.canonical_branch!r}; "
                "use `git-paoding init --replace` for guarded wrong-base recovery"
            )
        session, _replay_atoms, status = reconcile_and_status(repo, session)
        store.save(session)
        return status


def _head_branch_instruction(target: PullRequestTarget) -> str:
    return (
        f"Run `git fetch origin {target.head_ref_name}` and then "
        f"`git checkout {target.head_ref_name}` (or update that local branch to "
        f"{target.head_ref_oid}) before retrying. git-paoding never fetches automatically."
    )


def init_session_from_pr(
    repo: Path,
    target: PullRequestTarget,
    *,
    slice_pr_prefix: str = "slice",
) -> StatusResult:
    """Validate a PR against local objects and initialize from its merge base."""

    repository = repo.resolve()
    session = _build_pr_session(repository, target=target, slice_pr_prefix=slice_pr_prefix)
    return _create_session(repository, session)


def _build_pr_session(
    repo: Path,
    *,
    target: PullRequestTarget,
    slice_pr_prefix: str,
) -> Session:
    """Validate PR initialization inputs and return an unpersisted session."""

    if target.state is not PRState.OPEN:
        raise PullRequestInitializationError(
            f"PR #{target.number} is {target.state.value}; only open PRs can seed a session"
        )
    if target.is_cross_repository:
        raise PullRequestInitializationError(
            f"PR #{target.number} is a cross-repository PR; cross-repository PRs are not "
            "supported for initialization"
        )

    try:
        local_head_oid = rev_parse(repo, f"refs/heads/{target.head_ref_name}^{{commit}}")
    except GitCommandError as error:
        raise PullRequestInitializationError(
            f"Local head branch {target.head_ref_name!r} does not exist. "
            f"{_head_branch_instruction(target)}"
        ) from error
    if local_head_oid != target.head_ref_oid:
        raise PullRequestInitializationError(
            f"Local head branch {target.head_ref_name!r} is at {local_head_oid}, but PR "
            f"#{target.number} expects {target.head_ref_oid}. {_head_branch_instruction(target)}"
        )

    if not plumbing.object_exists(repo, target.base_ref_oid):
        raise PullRequestInitializationError(
            f"PR #{target.number} base OID {target.base_ref_oid} is not available locally. "
            f"Run `git fetch origin {target.base_ref_name}` before retrying; git-paoding "
            "never fetches automatically."
        )
    if not plumbing.object_exists(repo, target.head_ref_oid):
        raise PullRequestInitializationError(
            f"PR #{target.number} head OID {target.head_ref_oid} is not available locally. "
            f"{_head_branch_instruction(target)}"
        )

    try:
        pinned_base_oid = plumbing.merge_base(repo, target.base_ref_oid, target.head_ref_oid)
    except GitCommandError as error:
        raise PullRequestInitializationError(
            f"PR #{target.number} base OID {target.base_ref_oid} and head OID "
            f"{target.head_ref_oid} have no common ancestor"
        ) from error
    if not pinned_base_oid:
        raise PullRequestInitializationError(
            f"PR #{target.number} base OID {target.base_ref_oid} and head OID "
            f"{target.head_ref_oid} have no common ancestor"
        )

    local_diffstat = plumbing.diff_numstat(repo, pinned_base_oid, target.head_ref_oid)
    github_diffstat = (target.changed_files, target.additions, target.deletions)
    if local_diffstat != github_diffstat:
        raise PullRequestInitializationError(
            f"PR #{target.number} diffstat disagrees with local Git: GitHub reports "
            f"{github_diffstat[0]} files, +{github_diffstat[1]}, -{github_diffstat[2]}; "
            f"local Git reports {local_diffstat[0]} files, +{local_diffstat[1]}, "
            f"-{local_diffstat[2]}. Likely causes are stale local objects or "
            "rename/whitespace accounting differences."
        )

    return Session(
        canonical_branch=target.head_ref_name,
        base_ref=target.base_ref_name,
        base_oid=pinned_base_oid,
        slice_pr_prefix=slice_pr_prefix,
        integration_pr=target.number,
        source_pr=SourcePullRequest(
            number=target.number,
            url=target.url,
            base_ref_name=target.base_ref_name,
            base_ref_oid=target.base_ref_oid,
            head_ref_name=target.head_ref_name,
            head_ref_oid=target.head_ref_oid,
            merge_base_oid=pinned_base_oid,
        ),
    )


def _replacement_ref_prefix(canonical_branch: str) -> str:
    return f"refs/heads/paoding/{branch_key(canonical_branch)}/"


def _require_replaceable(repo: Path, session: Session) -> None:
    """Reject every durable or local sign that publication may have begun."""

    if session.publication_started:
        raise SessionReplacementError(
            "Cannot replace this session because publication has already started"
        )
    if any(slice_.pr_number is not None for slice_ in session.slices):
        raise SessionReplacementError(
            "Cannot replace this session because a slice pull request number is recorded"
        )
    ref_prefix = _replacement_ref_prefix(session.canonical_branch)
    if plumbing.for_each_ref(repo, ref_prefix):
        raise SessionReplacementError(
            f"Cannot replace this session because local paoding refs exist below {ref_prefix}"
        )
    if session.integration_pr is not None and session.source_pr is None:
        raise SessionReplacementError(
            "Cannot replace this legacy session because an integration pull request is recorded "
            "without source pull-request identity"
        )


def replace_session(
    repo: Path,
    *,
    base: str | None = None,
    pr_target: PullRequestTarget | None = None,
    canonical_branch: str | None = None,
    slice_pr_prefix: str = "slice",
) -> ReplaceResult:
    """Replace an unpublished session after creating an exact recovery backup."""

    if (base is None) == (pr_target is None):
        raise ValueError("Exactly one of base or pr_target is required")

    repository = repo.resolve()
    if pr_target is not None:
        branch = pr_target.head_ref_name
        if (
            canonical_branch is not None
            and _canonical_branch(repository, canonical_branch) != branch
        ):
            raise BranchResolutionError(
                f"PR head branch {branch!r} does not match canonical_branch {canonical_branch!r}"
            )
    else:
        branch = _canonical_branch(repository, canonical_branch)

    store = JsonSessionStore(repository)
    with SessionLock(repository, branch):
        try:
            old_session = store.load(branch)
        except SessionNotFoundError as error:
            raise SessionNotFoundError(
                f"No git-paoding session exists for branch {branch!r}; use plain "
                "`git-paoding init` instead of `git-paoding init --replace`"
            ) from error
        _require_replaceable(repository, old_session)

        if pr_target is not None:
            new_session = _build_pr_session(
                repository,
                target=pr_target,
                slice_pr_prefix=slice_pr_prefix,
            )
        else:
            assert base is not None
            new_session = _build_local_session(
                repository,
                base=base,
                canonical_branch=branch,
                slice_pr_prefix=slice_pr_prefix,
            )
        new_session, _replay_atoms, status = reconcile_and_status(repository, new_session)
        backup_path = store.backup(branch)
        store.save(new_session)
        return ReplaceResult(status=status, backup_path=backup_path)


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
    """Resolve selectors against the current diff and persist their slice ownership."""

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
    progress: ProgressCallback | None = None,
    network_timeout: float | None = 120.0,
) -> PublishResult:
    """Run the idempotent projection/ref/PR publication pipeline."""

    repository = repo.resolve()
    branch = _canonical_branch(repository, canonical_branch)
    return publish_session(
        repository,
        canonical_branch=branch,
        backend=backend,
        remote=remote,
        progress=progress,
        network_timeout=network_timeout,
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
    "InvalidBaseRefError",
    "publish",
    "remove_slice",
    "rename_slice",
    "set_focus",
]
