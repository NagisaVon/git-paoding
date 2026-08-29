"""Backend-neutral GitHub pull-request operations.

The protocol is intentionally small: core publishing code should know about
pull requests, but never about ``gh`` arguments or JSON response shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from git_paoding.core.model import PaodingError, PRRecord, PRState


class GitHubBackendError(PaodingError):
    """Base class for failures at the GitHub backend boundary."""


class DuplicatePullRequestMarkerError(GitHubBackendError):
    """Raised when one stable slice marker identifies multiple open PRs."""


class MissingPullRequestMarkerError(GitHubBackendError):
    """Raised when an upsert body would lose its recoverable identity."""


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


@dataclass(frozen=True, slots=True)
class PullRequestUpsert:
    """Result of adopting, updating, or creating one marker-owned draft PR."""

    pr: PRRecord
    created: bool
    updated: bool


def find_open_pr_by_marker(
    backend: GitHubBackend,
    marker: str,
) -> PRRecord | None:
    """Find the unique open PR containing ``marker`` in its body."""

    if not marker:
        raise MissingPullRequestMarkerError("Pull-request identity marker cannot be empty")
    matches = [pr for pr in backend.list_open_prs() if marker in pr.body]
    if len(matches) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in matches)
        raise DuplicatePullRequestMarkerError(
            f"Multiple open pull requests contain marker {marker!r}: {numbers}. "
            "Close or repair the duplicate before publishing."
        )
    return matches[0] if matches else None


def upsert_draft_pr_by_marker(
    backend: GitHubBackend,
    *,
    marker: str,
    title: str,
    body: str,
    base_ref: str,
    head_ref: str,
) -> PullRequestUpsert:
    """Adopt by marker before creating, preserving stable PR identity.

    This is the recovery path used when local slice-to-PR metadata is absent.
    It deliberately searches first on every call; creation is impossible until
    the search proves that the marker is absent.
    """

    if marker not in body:
        raise MissingPullRequestMarkerError(
            "Draft pull-request body must contain its machine-readable identity marker"
        )
    existing = find_open_pr_by_marker(backend, marker)
    if existing is None:
        created = backend.create_draft_pr(
            title=title,
            body=body,
            base_ref=base_ref,
            head_ref=head_ref,
        )
        return PullRequestUpsert(pr=created, created=True, updated=False)

    if existing.state is not PRState.OPEN:
        raise GitHubBackendError(
            f"Marker search returned closed pull request #{existing.number}; backend is invalid"
        )
    if existing.title == title and existing.body == body:
        return PullRequestUpsert(pr=existing, created=False, updated=False)

    updated = backend.update_pr(existing.number, title=title, body=body)
    return PullRequestUpsert(pr=updated, created=False, updated=True)
