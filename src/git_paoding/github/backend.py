"""Backend-neutral GitHub pull-request operations.

The protocol is intentionally small: core publishing code should know about
pull requests, but never about ``gh`` arguments or JSON response shapes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from git_paoding.core.model import PaodingError, PRRecord


class GitHubBackendError(PaodingError):
    """Base class for failures at the GitHub backend boundary."""


class DuplicatePullRequestMarkerError(GitHubBackendError):
    """Raised when one stable slice marker identifies multiple open PRs."""


class PullRequestNotFoundError(GitHubBackendError):
    """Raised when a requested pull-request identity no longer exists."""


@runtime_checkable
class GitHubBackend(Protocol):
    """Thin interface consumed by publish and archive orchestration."""

    def check_ready(self) -> None:
        """Verify that the backend is installed, supported, and authenticated."""

    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        base_ref: str,
        head_ref: str,
    ) -> PRRecord:
        """Create a draft pull request and return its backend-neutral record."""

    def update_pr(self, number: int, *, title: str, body: str) -> PRRecord:
        """Replace the title and body of an existing pull request."""

    def close_pr(self, number: int) -> PRRecord:
        """Close an existing pull request without deleting its history."""

    def get_pr(self, number: int) -> PRRecord:
        """Return one pull request by number."""

    def list_open_prs(self) -> list[PRRecord]:
        """Return open pull requests with body text available for marker search."""
