"""Shared pytest fixtures backed by real, temporary Git repositories."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

import pytest

from git_paoding.gitio.plumbing import GitIdentity, commit_tree, update_ref
from git_paoding.gitio.runner import run_git


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
