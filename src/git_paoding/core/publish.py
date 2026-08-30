"""T07's idempotent publish orchestration.

This module is the only core path that may push refs or mutate pull requests.
It intentionally implements the CP2 vertical-slice surface, leaving archive
and post-CP2 hardening to their scheduled tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.model import (
    AtomState,
    DiffStat,
    PaodingError,
    PRRecord,
    PRState,
    PublishOutcome,
    PublishResult,
    PublishSliceResult,
    Session,
    SessionSummary,
    SliceStatus,
    SliceSummary,
    StatusResult,
)
from git_paoding.core.projection import build_projection
from git_paoding.core.reconcile import reconcile
from git_paoding.github.backend import DuplicatePullRequestMarkerError, GitHubBackend
from git_paoding.github.prbody import (
    HUMAN_NARRATIVE_SCAFFOLD,
    rewrite_integration_body,
    rewrite_slice_body,
    slice_marker,
)
from git_paoding.gitio.diffparse import diff_trees
from git_paoding.gitio.plumbing import rev_parse
from git_paoding.gitio.refs import RefSyncResult, generated_refs, sync_projection_refs
from git_paoding.store.jsonstore import JsonSessionStore, branch_key
from git_paoding.store.lock import SessionLock


class PublishError(PaodingError):
    """Raised when publication state is inconsistent or unsafe to guess."""


@dataclass(frozen=True, slots=True)
class _PreparedSlice:
    """Projection refs prepared before any pull request is mutated."""

    base_ref: str
    head_ref: str
    ref_sync: RefSyncResult


def reconcile_and_status(
    repo: Path,
    session: Session,
) -> tuple[Session, tuple[ReplayAtom, ...], StatusResult]:
    """Reconcile a session against its live canonical tip and build status."""

    final_oid = rev_parse(repo, f"refs/heads/{session.canonical_branch}")
    replay_atoms = atomize_hunks(diff_trees(repo, session.base_oid, final_oid))
    reconciled_atoms = reconcile(session.atoms, tuple(item.atom for item in replay_atoms))
    session = session.model_copy(
        update={"atoms": list(reconciled_atoms), "last_final_oid": final_oid}
    )

    slice_summaries: list[SliceSummary] = []
    for slice_ in session.slices:
        owned = [atom for atom in session.atoms if atom.owner == slice_.id]
        slice_summaries.append(
            SliceSummary(
                id=slice_.id,
                title=slice_.title,
                status=slice_.status,
                pr_number=slice_.pr_number,
                diffstat=DiffStat(
                    files_changed=len({atom.path for atom in owned}),
                    additions=sum(atom.final_len for atom in owned),
                    deletions=sum(atom.base_len for atom in owned),
                ),
            )
        )

    status = StatusResult(
        session=SessionSummary(
            canonical_branch=session.canonical_branch,
            base_ref=session.base_ref,
            base_oid=session.base_oid,
            last_final_oid=session.last_final_oid,
            focus_slice=session.focus_slice,
            integration_pr=session.integration_pr,
        ),
        slices=slice_summaries,
        atoms=session.atoms,
        unassigned_count=sum(atom.state is AtomState.UNASSIGNED for atom in session.atoms),
        ambiguous_count=sum(atom.state is AtomState.AMBIGUOUS for atom in session.atoms),
    )
    return session, replay_atoms, status


def _short_ref(ref: str) -> str:
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def _integration_base_ref(session: Session, remote: str) -> str:
    # Assumes the session's base ref names a branch, plain or as
    # ``<remote>/<branch>`` for this publish remote; any other init --base
    # form (an OID, a tag, another remote) fails loudly at gh pr create.
    base_ref = session.base_ref or session.base_oid
    remote_prefix = f"{remote}/"
    if base_ref.startswith(remote_prefix):
        return base_ref[len(remote_prefix) :]
    return _short_ref(base_ref)


def _integration_title(session: Session) -> str:
    return f"[INTEGRATION] {session.canonical_branch}"


def _find_integration_pr(
    backend: GitHubBackend,
    session: Session,
    open_prs: list[PRRecord],
) -> PRRecord | None:
    if session.integration_pr is not None:
        stored = backend.get_pr(session.integration_pr)
        if stored.state is PRState.OPEN:
            if stored.head_ref != session.canonical_branch:
                raise PublishError(
                    f"Stored integration PR #{stored.number} has head {stored.head_ref!r}, "
                    f"expected {session.canonical_branch!r}"
                )
            return stored

    matches = [pr for pr in open_prs if pr.head_ref == session.canonical_branch]
    if len(matches) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in matches)
        raise PublishError(
            f"Multiple open PRs use canonical head {session.canonical_branch!r}: {numbers}"
        )
    return matches[0] if matches else None


def _ensure_integration_pr(
    backend: GitHubBackend,
    session: Session,
    *,
    remote: str,
    open_prs: list[PRRecord],
) -> PRRecord:
    existing = _find_integration_pr(backend, session, open_prs)
    if existing is not None:
        return existing

    initial_body = rewrite_integration_body(HUMAN_NARRATIVE_SCAFFOLD, slices=[])
    return backend.create_draft_pr(
        title=_integration_title(session),
        body=initial_body,
        base_ref=_integration_base_ref(session, remote),
        head_ref=session.canonical_branch,
    )


def _upsert_slice_pr(
    backend: GitHubBackend,
    *,
    slice_id: str,
    title: str,
    integration_pr_url: str,
    base_ref: str,
    head_ref: str,
    open_prs: list[PRRecord],
    currently_empty: bool = False,
) -> tuple[PRRecord, bool, bool]:
    marker = slice_marker(slice_id)
    matches = [pr for pr in open_prs if marker in pr.body]
    if len(matches) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in matches)
        raise DuplicatePullRequestMarkerError(
            f"Multiple open pull requests contain marker {marker!r}: {numbers}. "
            "Close or repair the duplicate before publishing."
        )
    existing = matches[0] if matches else None
    desired_title = f"[SLICE] {title}"

    if existing is None:
        body = rewrite_slice_body(
            HUMAN_NARRATIVE_SCAFFOLD,
            slice_id=slice_id,
            integration_pr_url=integration_pr_url,
            currently_empty=currently_empty,
        )
        created = backend.create_draft_pr(
            title=desired_title,
            body=body,
            base_ref=base_ref,
            head_ref=head_ref,
        )
        open_prs.append(created)
        return created, True, False

    body = rewrite_slice_body(
        existing.body,
        slice_id=slice_id,
        integration_pr_url=integration_pr_url,
        currently_empty=currently_empty,
    )
    if existing.title == desired_title and existing.body == body:
        return existing, False, False
    updated = backend.update_pr(existing.number, title=desired_title, body=body)
    open_prs[open_prs.index(existing)] = updated
    return updated, False, True


def _owned_replay_atoms(
    replay_atoms: tuple[ReplayAtom, ...], reconciled_session: Session
) -> tuple[ReplayAtom, ...]:
    """Pair reconciled ownership with current authoritative replay payloads."""

    if len(replay_atoms) != len(reconciled_session.atoms):
        raise PublishError("Reconciled atom metadata no longer matches current replay payloads")
    return tuple(
        replace(replay_atom, atom=atom)
        for replay_atom, atom in zip(replay_atoms, reconciled_session.atoms, strict=True)
    )


def publish_session(
    repo: Path,
    *,
    canonical_branch: str,
    backend: GitHubBackend,
    remote: str = "origin",
) -> PublishResult:
    """Publish all active slices, or return action-needed without remote effects."""

    repository = repo.resolve()
    store = JsonSessionStore(repository)
    with SessionLock(repository, canonical_branch):
        session = store.load(canonical_branch)
        session, replay_atoms, status = reconcile_and_status(repository, session)
        store.save(session)

        if status.unassigned_count or status.ambiguous_count:
            return PublishResult(action_needed=True, status=status)

        backend.check_ready()
        current_replay_atoms = _owned_replay_atoms(replay_atoms, session)

        # Prepare every non-empty active projection before mutating any pull
        # request. A projection or push failure can therefore never leave a
        # newly-created PR pointing at incomplete generated refs.
        prepared_slices: dict[str, _PreparedSlice] = {}
        for slice_ in session.slices:
            if slice_.status is not SliceStatus.ACTIVE:
                continue
            if not any(atom.owner == slice_.id for atom in session.atoms):
                continue
            if session.last_final_oid is None:
                raise PublishError("Reconciliation did not resolve a canonical final commit")

            refs = generated_refs(branch_key(session.canonical_branch), slice_.id)
            projection = build_projection(
                repository,
                base_oid=session.base_oid,
                final_oid=session.last_final_oid,
                slice_id=slice_.id,
                replay_atoms=current_replay_atoms,
            )
            prepared_slices[slice_.id] = _PreparedSlice(
                base_ref=_short_ref(refs.base),
                head_ref=_short_ref(refs.head),
                ref_sync=sync_projection_refs(
                    repository,
                    remote,
                    refs,
                    base_oid=projection.base_commit_oid,
                    head_oid=projection.head_commit_oid,
                ),
            )

        open_prs = backend.list_open_prs()
        integration_pr = _ensure_integration_pr(
            backend,
            session,
            remote=remote,
            open_prs=open_prs,
        )
        if integration_pr not in open_prs:
            open_prs.append(integration_pr)
        session = session.model_copy(update={"integration_pr": integration_pr.number})

        slice_results: list[PublishSliceResult] = []
        index_rows: list[tuple[str, str, str | None]] = []
        updated_slices = list(session.slices)

        for index, slice_ in enumerate(session.slices):
            if slice_.status is not SliceStatus.ACTIVE:
                slice_results.append(
                    PublishSliceResult(
                        slice_id=slice_.id,
                        title=slice_.title,
                        outcome=PublishOutcome.SKIPPED,
                        pr_number=slice_.pr_number,
                    )
                )
                continue

            owns_atoms = any(atom.owner == slice_.id for atom in session.atoms)
            refs = generated_refs(branch_key(session.canonical_branch), slice_.id)
            base_ref = _short_ref(refs.base)
            head_ref = _short_ref(refs.head)

            if not owns_atoms:
                existing = next(
                    (pr for pr in open_prs if slice_marker(slice_.id) in pr.body),
                    None,
                )
                if existing is None:
                    index_rows.append((slice_.id, slice_.title, None))
                    slice_results.append(
                        PublishSliceResult(
                            slice_id=slice_.id,
                            title=slice_.title,
                            outcome=PublishOutcome.EMPTY,
                        )
                    )
                    continue
                pr, _created, _updated = _upsert_slice_pr(
                    backend,
                    slice_id=slice_.id,
                    title=slice_.title,
                    integration_pr_url=integration_pr.url,
                    base_ref=base_ref,
                    head_ref=head_ref,
                    open_prs=open_prs,
                    currently_empty=True,
                )
                updated_slices[index] = slice_.model_copy(update={"pr_number": pr.number})
                index_rows.append((slice_.id, slice_.title, pr.url))
                slice_results.append(
                    PublishSliceResult(
                        slice_id=slice_.id,
                        title=slice_.title,
                        outcome=PublishOutcome.EMPTY,
                        pr_number=pr.number,
                        url=pr.url,
                    )
                )
                continue

            prepared = prepared_slices.get(slice_.id)
            if prepared is None:
                raise PublishError(f"Missing prepared projection for non-empty slice {slice_.id!r}")
            pr, created, body_updated = _upsert_slice_pr(
                backend,
                slice_id=slice_.id,
                title=slice_.title,
                integration_pr_url=integration_pr.url,
                base_ref=prepared.base_ref,
                head_ref=prepared.head_ref,
                open_prs=open_prs,
            )
            updated_slices[index] = slice_.model_copy(update={"pr_number": pr.number})
            index_rows.append((slice_.id, slice_.title, pr.url))
            if created:
                outcome = PublishOutcome.CREATED
            elif not prepared.ref_sync.is_no_op or body_updated:
                outcome = PublishOutcome.REFRESHED
            else:
                outcome = PublishOutcome.NO_OP
            slice_results.append(
                PublishSliceResult(
                    slice_id=slice_.id,
                    title=slice_.title,
                    outcome=outcome,
                    pr_number=pr.number,
                    url=pr.url,
                )
            )

        desired_integration_body = rewrite_integration_body(
            integration_pr.body,
            slices=index_rows,
        )
        desired_integration_title = _integration_title(session)
        if (
            integration_pr.title != desired_integration_title
            or integration_pr.body != desired_integration_body
        ):
            integration_pr = backend.update_pr(
                integration_pr.number,
                title=desired_integration_title,
                body=desired_integration_body,
            )

        session = session.model_copy(
            update={"slices": updated_slices, "integration_pr": integration_pr.number}
        )
        store.save(session)
        return PublishResult(
            slices=slice_results,
            integration_pr=integration_pr.number,
            integration_pr_url=integration_pr.url,
            action_needed=False,
        )
