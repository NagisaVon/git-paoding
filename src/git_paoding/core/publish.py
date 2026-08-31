"""Remote synchronization for semantic review sessions.

The functions here own generated ref updates and GitHub pull-request mutations.
All other core modules operate without remote side effects.
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
from git_paoding.github.backend import (
    DuplicatePullRequestMarkerError,
    GitHubBackend,
    PullRequestNotFoundError,
)
from git_paoding.github.lifecycle import (
    MergedSlicePullRequestError,
    archive_slice_pr,
    remove_slice_pr,
    rename_slice_pr,
)
from git_paoding.github.prbody import (
    IntegrationSliceLink,
    RelatedSliceLink,
    rewrite_integration_body,
    rewrite_slice_body,
    slice_marker,
)
from git_paoding.gitio.diffparse import diff_trees
from git_paoding.gitio.plumbing import rev_parse
from git_paoding.gitio.refs import (
    RefSyncResult,
    delete_projection_refs,
    generated_refs,
    sync_projection_refs,
)
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
    *,
    full: bool = False,
) -> tuple[Session, tuple[ReplayAtom, ...], StatusResult]:
    """Reconcile a session against its live canonical tip and build status."""

    final_oid = rev_parse(repo, f"refs/heads/{session.canonical_branch}")
    replay_atoms = atomize_hunks(diff_trees(repo, session.base_oid, final_oid))
    reconciled_atoms = reconcile(
        session.atoms,
        tuple(item.atom for item in replay_atoms),
        focus_slice=session.focus_slice,
    )
    atoms = list(reconciled_atoms)
    if full:
        atoms = [
            atom.model_copy(update={"preview": _full_preview(replay_atom)})
            for atom, replay_atom in zip(atoms, replay_atoms, strict=True)
        ]
    session = session.model_copy(update={"atoms": atoms, "last_final_oid": final_oid})
    status = status_from_session(
        session,
        defaulted_atom_ids=reconciled_atoms.defaulted_atom_ids,
    )
    return session, replay_atoms, status


def _full_preview(replay_atom: ReplayAtom) -> str:
    """Render every changed line from the authoritative current Git diff."""

    if not replay_atom.removed_lines and not replay_atom.added_lines:
        return replay_atom.atom.preview

    rendered: list[str] = []
    for prefix, lines in (("-", replay_atom.removed_lines), ("+", replay_atom.added_lines)):
        for line in lines:
            text = line.decode("utf-8", errors="replace")
            rendered.append(prefix + text)
            if text and not text.endswith("\n"):
                rendered.append("\n")
    return "".join(rendered).removesuffix("\n")


def status_from_session(
    session: Session,
    *,
    defaulted_atom_ids: tuple[str, ...] = (),
) -> StatusResult:
    """Build a public status report from already-authoritative session state."""

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
            slice_pr_prefix=session.slice_pr_prefix,
            last_final_oid=session.last_final_oid,
            focus_slice=session.focus_slice,
            integration_pr=session.integration_pr,
            archived=session.archived,
        ),
        slices=slice_summaries,
        atoms=session.atoms,
        unassigned_count=sum(atom.state is AtomState.UNASSIGNED for atom in session.atoms),
        ambiguous_count=sum(atom.state is AtomState.AMBIGUOUS for atom in session.atoms),
        defaulted_atom_ids=list(defaulted_atom_ids),
    )
    return status


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
    return session.canonical_branch


def _find_integration_pr(
    backend: GitHubBackend,
    session: Session,
    open_prs: list[PRRecord],
    *,
    remote: str,
) -> PRRecord | None:
    expected_base_ref = _integration_base_ref(session, remote)
    stored: PRRecord | None = None
    if session.integration_pr is not None:
        try:
            stored = backend.get_pr(session.integration_pr)
        except PullRequestNotFoundError:
            stored = None
        if stored is not None and stored.state is PRState.MERGED:
            raise PublishError(
                f"Integration pull request #{stored.number} is already merged; "
                "run `git-paoding archive` instead of publishing again"
            )
        if stored is not None and stored.state is PRState.OPEN:
            if stored.head_ref != session.canonical_branch:
                raise PublishError(
                    f"Stored integration PR #{stored.number} has head {stored.head_ref!r}, "
                    f"expected {session.canonical_branch!r}"
                )
            if stored.base_ref != expected_base_ref:
                raise PublishError(
                    f"Stored integration PR #{stored.number} has base {stored.base_ref!r}, "
                    f"expected {expected_base_ref!r} for canonical head "
                    f"{session.canonical_branch!r}. Retarget or close the PR, then publish again."
                )

    candidates = list(open_prs)
    if (
        stored is not None
        and stored.state is PRState.OPEN
        and all(pr.number != stored.number for pr in candidates)
    ):
        candidates.append(stored)

    head_matches = [pr for pr in candidates if pr.head_ref == session.canonical_branch]
    exact_matches = [pr for pr in head_matches if pr.base_ref == expected_base_ref]
    if len(exact_matches) > 1:
        numbers = ", ".join(
            f"#{pr.number}" for pr in sorted(exact_matches, key=lambda pr: pr.number)
        )
        raise PublishError(
            f"Multiple open PRs use canonical head {session.canonical_branch!r} and expected "
            f"base {expected_base_ref!r}: {numbers}. Close the duplicates, then publish again."
        )
    if exact_matches:
        return exact_matches[0]

    conflicts = sorted(head_matches, key=lambda pr: pr.number)
    if conflicts:
        details = ", ".join(f"#{pr.number} (base {pr.base_ref!r})" for pr in conflicts)
        raise PublishError(
            f"No open integration PR for canonical head {session.canonical_branch!r} targets "
            f"expected base {expected_base_ref!r}; conflicting PRs: {details}. Retarget or close "
            "the conflicting PRs, then publish again."
        )
    return None


def _create_integration_pr(
    backend: GitHubBackend,
    session: Session,
    *,
    remote: str,
) -> PRRecord:
    initial_body = rewrite_integration_body("", slices=[])
    return backend.create_draft_pr(
        title=_integration_title(session),
        body=initial_body,
        base_ref=_integration_base_ref(session, remote),
        head_ref=session.canonical_branch,
    )


def _find_slice_pr(
    backend: GitHubBackend,
    *,
    slice_id: str,
    stored_number: int | None,
    open_prs: list[PRRecord],
) -> PRRecord | None:
    """Resolve identity marker-first, then recover a damaged stored PR body."""

    marker = slice_marker(slice_id)
    matches = [pr for pr in open_prs if marker in pr.body]
    if len(matches) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in matches)
        raise DuplicatePullRequestMarkerError(
            f"Multiple open pull requests contain marker {marker!r}: {numbers}. "
            "Close or repair the duplicate before publishing."
        )
    if matches:
        return matches[0]
    if stored_number is None:
        return None
    try:
        stored = backend.get_pr(stored_number)
    except PullRequestNotFoundError:
        return None
    if stored.state is PRState.MERGED:
        raise MergedSlicePullRequestError(
            f"Slice pull request #{stored.number} is merged; generated review projections "
            "must only be closed, never merged"
        )
    return stored if stored.state is PRState.OPEN else None


def _slice_diffstat(session: Session, slice_id: str) -> DiffStat:
    owned = [atom for atom in session.atoms if atom.owner == slice_id]
    return DiffStat(
        files_changed=len({atom.path for atom in owned}),
        additions=sum(atom.final_len for atom in owned),
        deletions=sum(atom.base_len for atom in owned),
    )


def _related_links(
    session: Session,
    *,
    slice_id: str,
    prs: dict[str, PRRecord],
) -> tuple[RelatedSliceLink, ...]:
    owned_paths = {atom.path for atom in session.atoms if atom.owner == slice_id}
    links: list[RelatedSliceLink] = []
    for related in session.slices:
        if related.id == slice_id or related.status is not SliceStatus.ACTIVE:
            continue
        related_pr = prs.get(related.id)
        if related_pr is None:
            continue
        related_paths = {atom.path for atom in session.atoms if atom.owner == related.id}
        shared = tuple(sorted(owned_paths & related_paths))
        if shared:
            links.append(
                RelatedSliceLink(
                    number=related_pr.number,
                    title=related.title,
                    url=related_pr.url,
                    shared_paths=shared,
                )
            )
    return tuple(links)


def _commit_url(pr_url: str, oid: str) -> str:
    """Derive the repository commit URL from a GitHub pull-request URL."""

    for component in ("/pull/", "/pulls/"):
        if component in pr_url:
            return f"{pr_url.split(component, maxsplit=1)[0]}/commit/{oid}"
    return f"{pr_url.rstrip('/')}/commits/{oid}"


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
        if session.archived:
            raise PublishError("This review session is archived and cannot be published")
        session, replay_atoms, status = reconcile_and_status(repository, session)
        store.save(session)

        if status.unassigned_count or status.ambiguous_count:
            return PublishResult(action_needed=True, status=status)

        backend.check_ready()
        open_prs = backend.list_open_prs()
        existing_integration_pr = _find_integration_pr(
            backend,
            session,
            open_prs,
            remote=remote,
        )
        resolved_prs: dict[str, PRRecord] = {}
        for slice_ in session.slices:
            existing = _find_slice_pr(
                backend,
                slice_id=slice_.id,
                stored_number=slice_.pr_number,
                open_prs=open_prs,
            )
            if existing is not None:
                resolved_prs[slice_.id] = existing
        current_replay_atoms = _owned_replay_atoms(replay_atoms, session)

        # Prepare every active projection needed by a current pull request
        # before mutating any pull request. Brand-new empty slices need no
        # refs, while an existing slice that became empty needs a zero-diff
        # projection so its Files changed view cannot retain stale content.
        prepared_slices: dict[str, _PreparedSlice] = {}
        for slice_ in session.slices:
            if slice_.status is not SliceStatus.ACTIVE:
                continue
            owns_atoms = any(atom.owner == slice_.id for atom in session.atoms)
            if not owns_atoms and slice_.id not in resolved_prs:
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

        integration_pr = existing_integration_pr or _create_integration_pr(
            backend, session, remote=remote
        )
        if integration_pr not in open_prs:
            open_prs.append(integration_pr)
        session = session.model_copy(update={"integration_pr": integration_pr.number})

        created_slice_ids: set[str] = set()
        updated_slices = list(session.slices)
        branch = branch_key(session.canonical_branch)

        # Removed slices keep their stable record for archaeology. Publishing
        # closes the review PR before deleting its generated refs.
        for index, slice_ in enumerate(session.slices):
            if slice_.status is SliceStatus.ACTIVE:
                continue
            existing = resolved_prs.get(slice_.id)
            if existing is not None:
                closed = remove_slice_pr(backend, existing.number, slice_id=slice_.id)
                updated_slices[index] = slice_.model_copy(update={"pr_number": closed.number})
            delete_projection_refs(
                repository,
                remote,
                generated_refs(branch, slice_.id),
            )

        # Resolve every stable identity before creating anything. Missing
        # mappings are recovered from markers; stale stored numbers fall back
        # to that same marker result before a new PR is considered.
        for slice_ in session.slices:
            if slice_.status is not SliceStatus.ACTIVE:
                continue

            owns_atoms = any(atom.owner == slice_.id for atom in session.atoms)
            if not owns_atoms or slice_.id in resolved_prs:
                continue

            prepared = prepared_slices.get(slice_.id)
            if prepared is None:
                raise PublishError(f"Missing prepared projection for non-empty slice {slice_.id!r}")
            created = backend.create_draft_pr(
                title=f"[{session.slice_pr_prefix}] {slice_.title}",
                body=rewrite_slice_body(
                    "",
                    slice_id=slice_.id,
                    integration_pr_url=integration_pr.url,
                    diffstat=_slice_diffstat(session, slice_.id),
                ),
                base_ref=prepared.base_ref,
                head_ref=prepared.head_ref,
            )
            open_prs.append(created)
            resolved_prs[slice_.id] = created
            created_slice_ids.add(slice_.id)

        prior_prs = dict(resolved_prs)
        for slice_ in session.slices:
            if slice_.status is not SliceStatus.ACTIVE:
                continue
            existing = resolved_prs.get(slice_.id)
            if existing is None:
                continue
            refreshed = rename_slice_pr(
                backend,
                existing.number,
                slice_id=slice_.id,
                title=slice_.title,
                prefix=session.slice_pr_prefix,
                integration_pr_url=integration_pr.url,
                diffstat=_slice_diffstat(session, slice_.id),
                related_slices=_related_links(session, slice_id=slice_.id, prs=resolved_prs),
                currently_empty=not any(atom.owner == slice_.id for atom in session.atoms),
            )
            resolved_prs[slice_.id] = refreshed

        slice_results: list[PublishSliceResult] = []
        index_rows: list[IntegrationSliceLink] = []
        for index, slice_ in enumerate(session.slices):
            pr = resolved_prs.get(slice_.id)
            if slice_.status is not SliceStatus.ACTIVE:
                slice_results.append(
                    PublishSliceResult(
                        slice_id=slice_.id,
                        title=slice_.title,
                        outcome=PublishOutcome.SKIPPED,
                        pr_number=updated_slices[index].pr_number,
                    )
                )
                continue

            if pr is not None:
                updated_slices[index] = slice_.model_copy(update={"pr_number": pr.number})
            index_rows.append(
                IntegrationSliceLink(
                    slice_id=slice_.id,
                    title=slice_.title,
                    number=pr.number if pr is not None else None,
                    url=pr.url if pr is not None else None,
                )
            )
            owns_atoms = any(atom.owner == slice_.id for atom in session.atoms)
            if not owns_atoms:
                outcome = PublishOutcome.EMPTY
            elif slice_.id in created_slice_ids:
                outcome = PublishOutcome.CREATED
            elif not prepared_slices[slice_.id].ref_sync.is_no_op or prior_prs[slice_.id] != pr:
                outcome = PublishOutcome.REFRESHED
            else:
                outcome = PublishOutcome.NO_OP
            slice_results.append(
                PublishSliceResult(
                    slice_id=slice_.id,
                    title=slice_.title,
                    outcome=outcome,
                    pr_number=pr.number if pr is not None else None,
                    url=pr.url if pr is not None else None,
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
        defaulted_status = (
            status_from_session(
                session,
                defaulted_atom_ids=tuple(status.defaulted_atom_ids),
            )
            if status.defaulted_atom_ids
            else None
        )
        return PublishResult(
            slices=slice_results,
            integration_pr=integration_pr.number,
            integration_pr_url=integration_pr.url,
            action_needed=False,
            status=defaulted_status,
        )


def archive_session(
    repo: Path,
    *,
    canonical_branch: str,
    backend: GitHubBackend,
    remote: str = "origin",
) -> StatusResult:
    """Close slice PRs, delete their refs, and durably archive the session."""

    repository = repo.resolve()
    store = JsonSessionStore(repository)
    with SessionLock(repository, canonical_branch):
        session = store.load(canonical_branch)
        session, _replay_atoms, _status = reconcile_and_status(repository, session)
        backend.check_ready()
        open_prs = backend.list_open_prs()

        integration_pr: PRRecord | None = None
        if session.integration_pr is not None:
            try:
                integration_pr = backend.get_pr(session.integration_pr)
            except PullRequestNotFoundError:
                integration_pr = None
        if integration_pr is None:
            matches = [pr for pr in open_prs if pr.head_ref == session.canonical_branch]
            if len(matches) != 1:
                raise PublishError(
                    "Archive requires one identifiable integration PR for the canonical branch"
                )
            integration_pr = matches[0]
        if integration_pr.state is not PRState.MERGED:
            raise PublishError(
                f"Integration pull request #{integration_pr.number} must be merged before "
                f"archiving; current state is {integration_pr.state.value!r}"
            )
        if session.last_final_oid is None:
            raise PublishError("Reconciliation did not resolve a canonical final commit")

        updated_slices = list(session.slices)
        branch = branch_key(session.canonical_branch)
        archive_prs: dict[int, PRRecord] = {}
        for index, slice_ in enumerate(session.slices):
            marker_matches = [pr for pr in open_prs if slice_marker(slice_.id) in pr.body]
            if len(marker_matches) > 1:
                numbers = ", ".join(f"#{pr.number}" for pr in marker_matches)
                raise DuplicatePullRequestMarkerError(
                    f"Multiple open pull requests contain marker {slice_marker(slice_.id)!r}: "
                    f"{numbers}. Close or repair the duplicate before archiving."
                )
            slice_pr = marker_matches[0] if marker_matches else None
            if slice_pr is None and slice_.pr_number is not None:
                try:
                    slice_pr = backend.get_pr(slice_.pr_number)
                except PullRequestNotFoundError:
                    slice_pr = None
            if slice_pr is not None and slice_pr.state is PRState.MERGED:
                raise MergedSlicePullRequestError(
                    f"Slice pull request #{slice_pr.number} is merged; generated review "
                    "projections must only be closed, never merged"
                )
            if slice_pr is not None:
                archive_prs[index] = slice_pr

        for index, slice_ in enumerate(session.slices):
            slice_pr = archive_prs.get(index)
            if slice_pr is not None and slice_.status is SliceStatus.ACTIVE:
                archived_pr = archive_slice_pr(
                    backend,
                    slice_pr.number,
                    integration_pr_number=integration_pr.number,
                    integration_pr_url=integration_pr.url,
                    merged_commit=session.last_final_oid,
                    merged_commit_url=_commit_url(integration_pr.url, session.last_final_oid),
                )
                updated_slices[index] = slice_.model_copy(
                    update={"pr_number": archived_pr.number, "status": SliceStatus.ARCHIVED}
                )
            else:
                updated_slices[index] = slice_.model_copy(update={"status": SliceStatus.ARCHIVED})
            delete_projection_refs(
                repository,
                remote,
                generated_refs(branch, slice_.id),
            )

        session = session.model_copy(
            update={
                "slices": updated_slices,
                "focus_slice": None,
                "integration_pr": integration_pr.number,
                "archived": True,
            }
        )
        store.save(session)
        return status_from_session(session)
