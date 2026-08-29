"""Integration tests for the typed Git runner boundary."""

from __future__ import annotations

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.runner import GitCommandError, GitFailureKind, run_git


@pytest.mark.integration
def test_runner_applies_identity_environment(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({}, {})

    result = run_git(
        ("var", "GIT_AUTHOR_IDENT"),
        cwd=repo.path,
        env={
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
        },
    )

    assert result.stdout_text().startswith("Test Author <author@example.test>")


@pytest.mark.integration
def test_runner_maps_unknown_revision(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({}, {})

    with pytest.raises(GitCommandError) as raised:
        run_git(("rev-parse", "--verify", "missing-revision"), cwd=repo.path)

    assert raised.value.kind is GitFailureKind.UNKNOWN_REVISION
    assert raised.value.stderr
