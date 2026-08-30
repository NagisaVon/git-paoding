"""Backend-neutral pull-request lifecycle operations for review slices."""

from __future__ import annotations

from collections.abc import Sequence

from git_paoding.core.model import DiffStat, PRRecord, PRState, SliceId
from git_paoding.github.backend import GitHubBackend
from git_paoding.github.prbody import (
    RelatedSliceLink,
    rewrite_archived_slice_body,
    rewrite_removed_slice_body,
    rewrite_slice_body,
)


def _update_if_changed(
    backend: GitHubBackend,
    current: PRRecord,
    *,
    title: str,
    body: str,
) -> PRRecord:
    if current.title == title and current.body == body:
        return current
    return backend.update_pr(current.number, title=title, body=body)


def rename_slice_pr(
    backend: GitHubBackend,
    number: int,
    *,
    slice_id: SliceId | str,
    title: str,
    integration_pr_url: str,
    diffstat: DiffStat,
    related_slices: Sequence[RelatedSliceLink] = (),
    currently_empty: bool = False,
) -> PRRecord:
    """Rename and refresh a slice in place, preserving its PR identity."""

    current = backend.get_pr(number)
    desired_body = rewrite_slice_body(
        current.body,
        slice_id=slice_id,
        integration_pr_url=integration_pr_url,
        diffstat=diffstat,
        related_slices=related_slices,
        currently_empty=currently_empty,
    )
    return _update_if_changed(
        backend,
        current,
        title=f"[SLICE] {title}",
        body=desired_body,
    )


def remove_slice_pr(
    backend: GitHubBackend,
    number: int,
    *,
    slice_id: SliceId | str,
) -> PRRecord:
    """Close a removed slice after preserving its narrative and adding a note."""

    current = backend.get_pr(number)
    desired_body = rewrite_removed_slice_body(current.body, slice_id=slice_id)
    current = _update_if_changed(
        backend,
        current,
        title=current.title,
        body=desired_body,
    )
    if current.state is PRState.CLOSED:
        return current
    return backend.close_pr(current.number)


def archive_slice_pr(
    backend: GitHubBackend,
    number: int,
    *,
    integration_pr_number: int,
    integration_pr_url: str,
    merged_commit: str,
    merged_commit_url: str,
) -> PRRecord:
    """Close one projection with a durable pointer to merged integration state."""

    current = backend.get_pr(number)
    desired_body = rewrite_archived_slice_body(
        current.body,
        integration_pr_number=integration_pr_number,
        integration_pr_url=integration_pr_url,
        merged_commit=merged_commit,
        merged_commit_url=merged_commit_url,
    )
    current = _update_if_changed(
        backend,
        current,
        title=current.title,
        body=desired_body,
    )
    if current.state is PRState.CLOSED:
        return current
    return backend.close_pr(current.number)
