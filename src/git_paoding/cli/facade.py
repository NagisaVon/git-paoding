"""Typed, replaceable seam between the Click shell and the public facade.

Some command hooks are completed by the publish-integration owner. Keeping
their expected signatures here lets command parsing and rendering remain
independently testable without changing the frozen facade in this workstream.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, Protocol, cast

from git_paoding import api
from git_paoding.core.model import (
    AssignBatchRequest,
    AssignResult,
    PaodingError,
    PublishResult,
    StatusResult,
)
from git_paoding.github.backend import GitHubBackend


class FacadeUnavailableError(PaodingError):
    """Raised when a command awaits its integration-owned facade hook."""


class CliFacade(Protocol):
    """Facade calls consumed by the complete CLI surface."""

    def init_session(self, repo: Path, base: str, *, backend: GitHubBackend) -> StatusResult: ...

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
    ) -> PublishResult: ...

    def archive(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
    ) -> StatusResult: ...


def _integration_hook(name: str) -> object:
    function = getattr(api, name, None)
    if function is None:
        raise FacadeUnavailableError(
            f"The {name} facade hook is not available until publish integration is installed"
        )
    return function


class ApiFacade:
    """Default adapter over :mod:`git_paoding.api`."""

    def init_session(self, repo: Path, base: str, *, backend: GitHubBackend) -> StatusResult:
        return api.init_session(repo, base, backend=backend)

    def add_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult:
        return api.add_slice(repo, slice_id, title)

    def list_slices(self, repo: Path) -> StatusResult:
        return api.get_status(repo)

    def remove_slice(self, repo: Path, slice_id: str) -> StatusResult:
        function = cast("Callable[[Path, str], StatusResult]", _integration_hook("remove_slice"))
        return function(repo, slice_id)

    def rename_slice(self, repo: Path, slice_id: str, title: str) -> StatusResult:
        function = cast(
            "Callable[[Path, str, str], StatusResult]", _integration_hook("rename_slice")
        )
        return function(repo, slice_id, title)

    def get_status(self, repo: Path, *, full: bool) -> StatusResult:
        if full:
            function = cast("Callable[[Path], StatusResult]", _integration_hook("get_full_status"))
            return function(repo)
        return api.get_status(repo)

    def assign(
        self,
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult:
        function = api.assign
        if force and "force" not in inspect.signature(function).parameters:
            raise FacadeUnavailableError(
                "The force-aware assign facade hook is not available until publish integration "
                "is installed"
            )
        if force:
            callable_with_force = cast("Callable[..., AssignResult]", function)
            return callable_with_force(repo, slice_id, selectors, force=True)
        return function(repo, slice_id, selectors)

    def assign_batch(self, repo: Path, request: AssignBatchRequest) -> AssignResult:
        function = cast(
            "Callable[[Path, AssignBatchRequest], AssignResult]",
            _integration_hook("assign_batch"),
        )
        return function(repo, request)

    def set_focus(self, repo: Path, slice_id: str | None) -> StatusResult:
        function = cast(
            "Callable[[Path, str | None], StatusResult]", _integration_hook("set_focus")
        )
        return function(repo, slice_id)

    def publish(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
    ) -> PublishResult:
        return api.publish(repo, backend=backend, remote=remote)

    def archive(
        self,
        repo: Path,
        *,
        backend: GitHubBackend,
        remote: str,
    ) -> StatusResult:
        function = cast("Callable[..., StatusResult]", _integration_hook("archive"))
        return function(repo, backend=backend, remote=remote)
