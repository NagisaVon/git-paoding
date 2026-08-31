"""Integration tests for the typed Git runner boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.runner import GitCommandError, GitFailureKind, GitTimeoutError, run_git


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


@pytest.mark.integration
def test_runner_timeout_names_only_the_git_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    secret_url = "https://credential@example.test/repository.git"

    with pytest.raises(GitTimeoutError) as raised:
        run_git(("push", secret_url, "secret-ref"), cwd=tmp_path, timeout=0.01)

    assert str(raised.value) == "git push timed out after 0.01 seconds"
    assert secret_url not in str(raised.value)
    assert "secret-ref" not in str(raised.value)
