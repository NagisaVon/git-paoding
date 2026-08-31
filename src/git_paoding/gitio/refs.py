"""Generated projection-ref naming and atomic remote synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, NoReturn, Sequence

from git_paoding.core.model import PaodingError
from git_paoding.gitio.plumbing import ls_remote, update_refs_transaction
from git_paoding.gitio.runner import GitCommandError, run_git

_PER_SLICE_FALLBACK_ENABLED: Final = False


class ConcurrentPublisherError(PaodingError):
    """Raised when an exact remote-ref lease detects another publisher."""


class AtomicPushUnsupportedError(PaodingError):
    """Raised when a remote cannot apply the required all-or-nothing update."""


@dataclass(frozen=True, slots=True)
class GeneratedRefs:
    """The two branch refs backing one slice pull request."""

    base: str
    head: str


@dataclass(frozen=True, slots=True)
class BatchRefSyncResult:
    """Desired projection refs and the subset changed on the remote."""

    desired: dict[str, str]
    pushed_refs: tuple[str, ...]

    @property
    def is_no_op(self) -> bool:
        """Return whether the remote already advertised every desired OID."""

        return not self.pushed_refs

    def slice_no_op(self, refs: GeneratedRefs) -> bool:
        """Return whether neither ref for one slice required a remote update."""

        return refs.base not in self.pushed_refs and refs.head not in self.pushed_refs


@dataclass(frozen=True, slots=True)
class RefSyncResult:
    """Which generated refs required a remote repair or refresh."""

    refs: GeneratedRefs
    base_pushed: bool
    head_pushed: bool

    @property
    def is_no_op(self) -> bool:
        """Return whether the remote already advertised both desired OIDs."""

        return not self.base_pushed and not self.head_pushed


@dataclass(frozen=True, slots=True)
class BatchRefDeleteResult:
    """Generated refs that existed remotely and were deleted."""

    deleted_refs: tuple[str, ...]

    @property
    def is_no_op(self) -> bool:
        """Return whether every requested generated ref was already absent."""

        return not self.deleted_refs


@dataclass(frozen=True, slots=True)
class RefDeleteResult:
    """Which generated refs existed remotely and were deleted."""

    refs: GeneratedRefs
    base_deleted: bool
    head_deleted: bool

    @property
    def is_no_op(self) -> bool:
        """Return whether the remote already lacked both generated refs."""

        return not self.base_deleted and not self.head_deleted


def generated_refs(branch_key: str, slice_id: str) -> GeneratedRefs:
    """Return generated base and head ref names for one branch and review slice."""

    if not branch_key or "/" in branch_key:
        raise ValueError("branch_key must be a non-empty single ref component")
    if not slice_id or "/" in slice_id:
        raise ValueError("slice_id must be a non-empty single ref component")
    prefix = f"refs/heads/paoding/{branch_key}/{slice_id}"
    return GeneratedRefs(base=f"{prefix}/base", head=f"{prefix}/head")


def update_local_projection_refs(
    repo: Path,
    refs: GeneratedRefs,
    *,
    base_oid: str,
    head_oid: str,
) -> None:
    """Point both local generated refs at deterministic projection commits atomically."""

    update_refs_transaction(repo, {refs.base: base_oid, refs.head: head_oid})


def sync_projection_refs_batch(
    repo: Path,
    remote: str,
    desired: Mapping[str, str],
    *,
    timeout: float | None = None,
) -> BatchRefSyncResult:
    """Synchronize all desired projection refs with one advertisement and push."""

    desired_refs = dict(desired)
    if not desired_refs:
        return BatchRefSyncResult(desired=desired_refs, pushed_refs=())

    advertisement_pattern = _projection_ref_glob(tuple(desired_refs))
    update_refs_transaction(repo, desired_refs)
    advertised = {
        item.ref: item.oid
        for item in ls_remote(repo, remote, advertisement_pattern, timeout=timeout)
    }
    changed = tuple(
        ref for ref, desired_oid in desired_refs.items() if advertised.get(ref) != desired_oid
    )
    if changed:
        try:
            _push_atomic_ref_updates(
                repo,
                remote,
                desired=desired_refs,
                observed=advertised,
                changed=changed,
                timeout=timeout,
            )
        except AtomicPushUnsupportedError:
            if not _PER_SLICE_FALLBACK_ENABLED:
                raise
            _push_ref_updates_individually(
                repo,
                remote,
                desired=desired_refs,
                observed=advertised,
                changed=changed,
                timeout=timeout,
            )
    return BatchRefSyncResult(desired=desired_refs, pushed_refs=changed)


def sync_projection_refs(
    repo: Path,
    remote: str,
    refs: GeneratedRefs,
    *,
    base_oid: str,
    head_oid: str,
    timeout: float | None = None,
) -> RefSyncResult:
    """Synchronize one slice while preserving the original per-slice result shape."""

    batch = sync_projection_refs_batch(
        repo,
        remote,
        {refs.base: base_oid, refs.head: head_oid},
        timeout=timeout,
    )
    return RefSyncResult(
        refs=refs,
        base_pushed=refs.base in batch.pushed_refs,
        head_pushed=refs.head in batch.pushed_refs,
    )


def delete_projection_refs_batch(
    repo: Path,
    remote: str,
    refs: Sequence[GeneratedRefs],
    *,
    timeout: float | None = None,
) -> BatchRefDeleteResult:
    """Delete generated refs with one remote advertisement and atomic push."""

    ordered_refs = tuple(ref for pair in refs for ref in (pair.head, pair.base))
    if not ordered_refs:
        return BatchRefDeleteResult(deleted_refs=())

    advertisement_pattern = _projection_ref_glob(ordered_refs)
    advertised = {
        item.ref: item.oid
        for item in ls_remote(repo, remote, advertisement_pattern, timeout=timeout)
    }
    existing = tuple(ref for ref in ordered_refs if ref in advertised)
    if existing:
        _push_atomic_ref_deletes(
            repo,
            remote,
            observed=advertised,
            refs=existing,
            timeout=timeout,
        )
    update_refs_transaction(repo, dict.fromkeys(ordered_refs))
    return BatchRefDeleteResult(deleted_refs=existing)


def delete_projection_refs(
    repo: Path,
    remote: str,
    refs: GeneratedRefs,
    *,
    timeout: float | None = None,
) -> RefDeleteResult:
    """Delete one archived slice while preserving the original result shape."""

    batch = delete_projection_refs_batch(repo, remote, (refs,), timeout=timeout)
    return RefDeleteResult(
        refs=refs,
        base_deleted=refs.base in batch.deleted_refs,
        head_deleted=refs.head in batch.deleted_refs,
    )


def _projection_ref_glob(refs: Sequence[str]) -> str:
    branch_prefix: str | None = None
    for ref in refs:
        components = ref.split("/")
        if (
            len(components) != 6
            or components[:3] != ["refs", "heads", "paoding"]
            or components[5] not in {"base", "head"}
            or not components[3]
            or not components[4]
        ):
            raise ValueError(f"Invalid generated projection ref: {ref!r}")
        candidate = "/".join(components[:4])
        if branch_prefix is None:
            branch_prefix = candidate
        elif candidate != branch_prefix:
            raise ValueError("All generated projection refs must belong to one branch")
    if branch_prefix is None:
        raise ValueError("At least one generated projection ref is required")
    return f"{branch_prefix}/*"


def _push_atomic_ref_updates(
    repo: Path,
    remote: str,
    *,
    desired: Mapping[str, str],
    observed: Mapping[str, str],
    changed: Sequence[str],
    timeout: float | None,
) -> None:
    args = ["push", "--atomic"]
    args.extend(_lease_argument(ref, observed.get(ref)) for ref in changed)
    args.append(remote)
    args.extend(f"{desired[ref]}:{ref}" for ref in changed)
    try:
        run_git(args, cwd=repo, timeout=timeout)
    except GitCommandError as error:
        _raise_mapped_push_error(error)


def _push_atomic_ref_deletes(
    repo: Path,
    remote: str,
    *,
    observed: Mapping[str, str],
    refs: Sequence[str],
    timeout: float | None,
) -> None:
    args = ["push", "--atomic"]
    args.extend(_lease_argument(ref, observed[ref]) for ref in refs)
    args.append(remote)
    args.extend(f":{ref}" for ref in refs)
    try:
        run_git(args, cwd=repo, timeout=timeout)
    except GitCommandError as error:
        _raise_mapped_push_error(error)


def _push_ref_updates_individually(
    repo: Path,
    remote: str,
    *,
    desired: Mapping[str, str],
    observed: Mapping[str, str],
    changed: Sequence[str],
    timeout: float | None,
) -> None:
    """Retain a disabled compatibility escape hatch for non-atomic remotes."""

    for ref in changed:
        try:
            run_git(
                (
                    "push",
                    _lease_argument(ref, observed.get(ref)),
                    remote,
                    f"{desired[ref]}:{ref}",
                ),
                cwd=repo,
                timeout=timeout,
            )
        except GitCommandError as error:
            _raise_mapped_push_error(error)


def _lease_argument(ref: str, observed_oid: str | None) -> str:
    return f"--force-with-lease={ref}:{observed_oid or ''}"


def _raise_mapped_push_error(error: GitCommandError) -> NoReturn:
    diagnostic = error.stderr.casefold()
    if "stale info" in diagnostic or "[rejected]" in diagnostic:
        raise ConcurrentPublisherError(
            "Generated refs changed after they were advertised. Confirm that only one "
            "publisher is running, then re-run the publish operation."
        ) from error
    if any(
        marker in diagnostic
        for marker in (
            "does not support --atomic",
            "atomic push is not supported",
            "atomic push failed",
        )
    ):
        raise AtomicPushUnsupportedError(
            "The remote does not support the required atomic generated-ref update."
        ) from error
    raise error
