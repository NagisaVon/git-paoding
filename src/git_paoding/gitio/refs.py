"""Generated projection-ref naming and idempotent remote synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git_paoding.gitio.plumbing import ls_remote, update_ref
from git_paoding.gitio.runner import run_git


@dataclass(frozen=True, slots=True)
class GeneratedRefs:
    """The two branch refs backing one slice pull request."""

    base: str
    head: str


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


def generated_refs(branch_key: str, slice_id: str) -> GeneratedRefs:
    """Return the generated base and head ref names for a review slice."""

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
    """Point both local generated refs at deterministic projection commits."""

    update_ref(repo, refs.base, base_oid)
    update_ref(repo, refs.head, head_oid)


def _force_push(repo: Path, remote: str, ref: str) -> None:
    run_git(("push", "--force", remote, f"{ref}:{ref}"), cwd=repo)


def sync_projection_refs(
    repo: Path,
    remote: str,
    refs: GeneratedRefs,
    *,
    base_oid: str,
    head_oid: str,
) -> RefSyncResult:
    """Repair remote projection refs using one authoritative ``ls-remote``.

    Local refs are updated from the deterministic desired OIDs, but each remote
    push occurs only when the batched advertisement differs.  Base is always
    pushed before head so an interrupted publication is safely repairable by a
    later call without any session-side OID cache.
    """

    update_local_projection_refs(
        repo,
        refs,
        base_oid=base_oid,
        head_oid=head_oid,
    )
    advertised = {item.ref: item.oid for item in ls_remote(repo, remote, refs.base, refs.head)}
    base_pushed = advertised.get(refs.base) != base_oid
    head_pushed = advertised.get(refs.head) != head_oid

    if base_pushed:
        _force_push(repo, remote, refs.base)
    if head_pushed:
        _force_push(repo, remote, refs.head)

    return RefSyncResult(
        refs=refs,
        base_pushed=base_pushed,
        head_pushed=head_pushed,
    )
