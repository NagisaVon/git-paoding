"""Typed, replaceable seam between the Click shell and the public API."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from git_paoding import api
from git_paoding.core.model import (
    AssignBatchRequest,
    AssignResult,
    PublishResult,
    PullRequestTarget,
    StatusResult,
)
from git_paoding.core.progress import ProgressCallback
from git_paoding.github.backend import GitHubBackend


class CliFacade(Protocol):
    """Facade calls consumed by the complete CLI surface."""

    def init_session(
        self,
        repo: Path,
        base: str,
        *,
        slice_pr_prefix: str = "slice",
    ) -> StatusResult: ...

    def init_session_from_pr(
        self,
        repo: Path,
        target: PullRequestTarget,
        *,
        slice_pr_prefix: str = "slice",
    ) -> StatusResult: ...

    def add_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult: ...

    def list_slices(self, repo: Path) -> StatusResult: ...

    def remove_slice(self, repo: Path, slice_id: str) -> StatusResult: ...

    def rename_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult: ...

    def get_status(self, repo: Path, *, full: bool) -> StatusResult: ...

    def assign(
        self,
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult: ...

    def assign_batch(self, repo: Path, request: AssignBatchRequest) -> AssignResult: ...

    def set_focus(self, repo: Path, slice_id: str | None) -> StatusResult: ...

    def publish(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
        progress: ProgressCallback | None = None,
        network_timeout: float | None = 120.0,
    ) -> PublishResult: ...

    def archive(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
    ) -> StatusResult: ...


class ApiFacade:
    """Default adapter over :mod:`git_paoding.api`."""

    def init_session(
        self,
        repo: Path,
        base: str,
        *,
        slice_pr_prefix: str = "slice",
    ) -> StatusResult:
        return api.init_session(repo, base, slice_pr_prefix=slice_pr_prefix)

    def init_session_from_pr(
        self,
        repo: Path,
        target: PullRequestTarget,
        *,
        slice_pr_prefix: str = "slice",
    ) -> StatusResult:
        return api.init_session_from_pr(
            repo,
            target,
            slice_pr_prefix=slice_pr_prefix,
        )

    def add_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult:
        return api.add_slice(repo, slice_id, title)

    def list_slices(self, repo: Path) -> StatusResult:
        return api.get_status(repo)

    def remove_slice(self, repo: Path, slice_id: str) -> StatusResult:
        return api.remove_slice(repo, slice_id)

    def rename_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult:
        return api.rename_slice(repo, slice_id, title)

    def get_status(self, repo: Path, *, full: bool) -> StatusResult:
        if full:
            return api.get_full_status(repo)
        return api.get_status(repo)

    def assign(
        self,
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult:
        return api.assign(repo, slice_id, selectors, force=force)

    def assign_batch(self, repo: Path, request: AssignBatchRequest) -> AssignResult:
        return api.assign_batch(repo, request)

    def set_focus(self, repo: Path, slice_id: str | None) -> StatusResult:
        return api.set_focus(repo, slice_id)

    def publish(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
        progress: ProgressCallback | None = None,
        network_timeout: float | None = 120.0,
    ) -> PublishResult:
        return api.publish(
            repo,
            backend=backend,
            remote=remote,
            progress=progress,
            network_timeout=network_timeout,
        )

    def archive(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
    ) -> StatusResult:
        return api.archive(repo, backend=backend, remote=remote)
