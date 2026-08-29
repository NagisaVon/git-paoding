"""Shared pytest fixtures backed by real, temporary Git repositories."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias

import pytest

from git_paoding.core.model import PRRecord, PRState
from git_paoding.github.backend import GitHubBackendError
from git_paoding.gitio.plumbing import GitIdentity, commit_tree, update_ref
from git_paoding.gitio.runner import run_git


@dataclass(slots=True)
class FakeBackend:
    """Deterministic GitHub backend for tests that do not call live GitHub."""

    prs: dict[int, PRRecord] = field(default_factory=dict)
    next_number: int = 1
    ready_checks: int = 0
    creates: list[int] = field(default_factory=list)
    updates: list[int] = field(default_factory=list)
    closes: list[int] = field(default_factory=list)
    lists: int = 0

    def check_ready(self) -> None:
        self.ready_checks += 1

    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        base_ref: str,
        head_ref: str,
    ) -> PRRecord:
        number = self.next_number
        self.next_number += 1
        pr = PRRecord(
            number=number,
            url=f"https://example.test/pulls/{number}",
            title=title,
            body=body,
            state=PRState.OPEN,
            is_draft=True,
            base_ref=base_ref,
            head_ref=head_ref,
        )
        self.prs[number] = pr
        self.creates.append(number)
        return pr

    def update_pr(self, number: int, *, title: str, body: str) -> PRRecord:
        current = self.get_pr(number)
        updated = current.model_copy(update={"title": title, "body": body})
        self.prs[number] = updated
        self.updates.append(number)
        return updated

    def close_pr(self, number: int) -> PRRecord:
        current = self.get_pr(number)
        closed = current.model_copy(update={"state": PRState.CLOSED})
        self.prs[number] = closed
        self.closes.append(number)
        return closed

    def get_pr(self, number: int) -> PRRecord:
        try:
            return self.prs[number]
        except KeyError as error:
            raise GitHubBackendError(f"Pull request #{number} does not exist") from error

    def list_open_prs(self) -> list[PRRecord]:
        self.lists += 1
        return [pr for pr in self.prs.values() if pr.state is PRState.OPEN]

    def seed(self, pr: PRRecord) -> None:
        """Add a pre-existing PR and keep generated numbers collision-free."""

        self.prs[pr.number] = pr
        self.next_number = max(self.next_number, pr.number + 1)


@dataclass(frozen=True, slots=True)
class RepoFile:
    """A file state for the scratch repository builder."""

    content: str | bytes
    executable: bool = False
    symlink: bool = False


FileValue: TypeAlias = str | bytes | RepoFile
RepoState: TypeAlias = Mapping[str, FileValue]


@dataclass(frozen=True, slots=True)
class ScratchRepository:
    """A two-commit scratch repository and its important object IDs."""

    path: Path
    base_oid: str
    final_oid: str
    base_tree_oid: str
    final_tree_oid: str


class ScratchRepoFactory(Protocol):
    """Create a scratch repository from Base and Final file maps."""

    def __call__(self, base: RepoState, final: RepoState) -> ScratchRepository: ...


@pytest.fixture
def fake_backend() -> FakeBackend:
    """Return a fresh backend with no live GitHub dependency."""

    return FakeBackend()


@dataclass(frozen=True, slots=True)
class _IsolationSnapshot:
    head_oid: bytes
    index_entries: bytes
    status: bytes


@dataclass(frozen=True, slots=True)
class _RegisteredRepository:
    repo: ScratchRepository
    snapshot: _IsolationSnapshot


def _snapshot(repo: Path) -> _IsolationSnapshot:
    return _IsolationSnapshot(
        head_oid=run_git(("rev-parse", "HEAD"), cwd=repo).stdout,
        index_entries=run_git(("ls-files", "--stage", "-z"), cwd=repo).stdout,
        status=run_git(("status", "--porcelain=v1", "-z"), cwd=repo).stdout,
    )


def _clear_worktree(repo: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _materialize_state(repo: Path, state: RepoState) -> None:
    _clear_worktree(repo)
    for relative_path, value in state.items():
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Scratch repository path must be relative and contained: {path}")
        spec = value if isinstance(value, RepoFile) else RepoFile(content=value)
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if spec.symlink:
            if not isinstance(spec.content, str):
                raise TypeError("A symlink target must be text")
            destination.symlink_to(spec.content)
            continue
        data = spec.content.encode() if isinstance(spec.content, str) else spec.content
        destination.write_bytes(data)
        if spec.executable:
            destination.chmod(destination.stat().st_mode | 0o111)


@pytest.fixture
def _repo_registry() -> list[_RegisteredRepository]:
    return []


@pytest.fixture
def scratch_repo_factory(
    tmp_path: Path, _repo_registry: list[_RegisteredRepository]
) -> ScratchRepoFactory:
    """Build clean Base/Final commits and register them for isolation checks."""

    counter = 0

    def factory(base: RepoState, final: RepoState) -> ScratchRepository:
        nonlocal counter
        counter += 1
        repo = tmp_path / f"repo-{counter}"
        repo.mkdir()
        run_git(("init", "--quiet", "--initial-branch=main"), cwd=repo)

        identity = GitIdentity(
            name="git-paoding tests",
            email="git-paoding@localhost",
            date="2000-01-01T00:00:00+00:00",
        )
        _materialize_state(repo, base)
        run_git(("add", "--all"), cwd=repo)
        base_tree_oid = run_git(("write-tree",), cwd=repo).stdout_text().strip()
        base_oid = commit_tree(
            repo,
            base_tree_oid,
            "Base\n",
            author=identity,
            committer=identity,
        )
        update_ref(repo, "refs/heads/main", base_oid)

        _materialize_state(repo, final)
        run_git(("add", "--all"), cwd=repo)
        final_tree_oid = run_git(("write-tree",), cwd=repo).stdout_text().strip()
        final_identity = GitIdentity(
            name=identity.name,
            email=identity.email,
            date="2000-01-01T00:00:01+00:00",
        )
        final_oid = commit_tree(
            repo,
            final_tree_oid,
            "Final\n",
            parents=(base_oid,),
            author=final_identity,
            committer=final_identity,
        )
        update_ref(repo, "refs/heads/main", final_oid, old_oid=base_oid)

        scratch_repo = ScratchRepository(
            path=repo,
            base_oid=base_oid,
            final_oid=final_oid,
            base_tree_oid=base_tree_oid,
            final_tree_oid=final_tree_oid,
        )
        _repo_registry.append(
            _RegisteredRepository(repo=scratch_repo, snapshot=_snapshot(scratch_repo.path))
        )
        return scratch_repo

    return factory


@pytest.fixture(autouse=True)
def _assert_integration_repository_isolation(
    request: pytest.FixtureRequest,
    _repo_registry: list[_RegisteredRepository],
) -> Iterator[None]:
    """Assert integration tests leave HEAD, index, and worktree status unchanged."""

    yield
    if request.node.get_closest_marker("integration") is None:
        return
    for registered in _repo_registry:
        assert _snapshot(registered.repo.path) == registered.snapshot
