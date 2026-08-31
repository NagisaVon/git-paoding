"""Local initialization validation and backend compatibility tests."""

from __future__ import annotations

import pytest

from conftest import ScratchRepoFactory
from git_paoding.api import InvalidBaseRefError, init_session
from git_paoding.gitio.runner import run_git


@pytest.mark.unit
@pytest.mark.parametrize("base", ["base", "origin/base"])
def test_init_accepts_local_and_remote_tracking_branches(
    scratch_repo_factory: ScratchRepoFactory,
    base: str,
) -> None:
    scratch = scratch_repo_factory(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    run_git(("update-ref", "refs/remotes/origin/base", scratch.base_oid), cwd=scratch.path)

    result = init_session(scratch.path, base)

    assert result.session.base_ref == base
    assert result.session.base_oid == scratch.base_oid


@pytest.mark.unit
@pytest.mark.parametrize("base_kind", ["oid", "tag"])
def test_init_rejects_non_branch_base_refs(
    scratch_repo_factory: ScratchRepoFactory,
    base_kind: str,
) -> None:
    scratch = scratch_repo_factory(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
    )
    if base_kind == "tag":
        run_git(("tag", "base-tag", scratch.base_oid), cwd=scratch.path)
        base = "base-tag"
    else:
        base = scratch.base_oid

    with pytest.raises(InvalidBaseRefError, match=r"branch or remote-tracking branch.*init --pr"):
        init_session(scratch.path, base)


@pytest.mark.unit
def test_deprecated_backend_argument_is_ignored(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = scratch_repo_factory(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)

    class FailingBackend:
        def check_ready(self) -> None:
            raise AssertionError("deprecated backend must not be called")

    backend = FailingBackend()
    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        result = init_session(scratch.path, "base", backend=backend)  # type: ignore[arg-type]

    assert result.session.base_oid == scratch.base_oid
