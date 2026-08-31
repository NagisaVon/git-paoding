"""Initialization from existing pull-request metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeBackend, ScratchRepoFactory, ScratchRepository
from git_paoding.api import (
    PullRequestInitializationError,
    add_slice,
    assign,
    init_session_from_pr,
    publish,
)
from git_paoding.core.model import PRRecord, PRState, PullRequestTarget
from git_paoding.gitio.plumbing import GitIdentity, commit_tree, diff_numstat
from git_paoding.gitio.runner import run_git
from git_paoding.store.jsonstore import JsonSessionStore


def _target(scratch: ScratchRepository, **changes: object) -> PullRequestTarget:
    files, additions, deletions = diff_numstat(scratch.path, scratch.base_oid, scratch.final_oid)
    target = PullRequestTarget(
        number=73,
        url="https://github.com/example/project/pull/73",
        state=PRState.OPEN,
        is_cross_repository=False,
        base_ref_name="target",
        base_ref_oid=scratch.base_oid,
        head_ref_name="main",
        head_ref_oid=scratch.final_oid,
        changed_files=files,
        additions=additions,
        deletions=deletions,
    )
    return target.model_copy(update=changes)


def _scratch(scratch_repo_factory: ScratchRepoFactory) -> ScratchRepository:
    return scratch_repo_factory(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"state": PRState.CLOSED}, r"PR #73 is closed; only open PRs"),
        ({"is_cross_repository": True}, r"cross-repository PRs are not supported"),
        ({"head_ref_name": "absent"}, r"git fetch origin absent.*git checkout absent"),
        ({"head_ref_oid": "f" * 40}, r"Local head branch 'main' is at .*expects f+"),
        ({"base_ref_oid": "e" * 40}, r"git fetch origin target"),
    ],
)
def test_init_from_pr_rejects_validation_failures_in_order(
    scratch_repo_factory: ScratchRepoFactory,
    changes: dict[str, object],
    message: str,
) -> None:
    scratch = _scratch(scratch_repo_factory)

    with pytest.raises(PullRequestInitializationError, match=message):
        init_session_from_pr(scratch.path, _target(scratch, **changes))

    assert not JsonSessionStore(scratch.path).exists("main")


@pytest.mark.integration
def test_init_from_pr_rejects_commits_without_a_common_ancestor(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    identity = GitIdentity(name="test", email="test@example.com")
    unrelated_oid = commit_tree(
        scratch.path,
        scratch.base_tree_oid,
        "Unrelated root\n",
        author=identity,
        committer=identity,
    )

    with pytest.raises(PullRequestInitializationError, match="have no common ancestor"):
        init_session_from_pr(
            scratch.path,
            _target(scratch, base_ref_oid=unrelated_oid),
        )

    assert not JsonSessionStore(scratch.path).exists("main")


@pytest.mark.integration
def test_diffstat_mismatch_reports_both_summaries_and_writes_no_session(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)

    with pytest.raises(PullRequestInitializationError) as caught:
        init_session_from_pr(
            scratch.path,
            _target(scratch, changed_files=8, additions=80, deletions=18),
        )

    message = str(caught.value)
    assert "GitHub reports 8 files, +80, -18" in message
    assert "local Git reports 1 files, +1, -1" in message
    assert "stale local objects" in message
    assert "rename/whitespace accounting differences" in message
    assert not JsonSessionStore(scratch.path).exists("main")


@pytest.mark.integration
def test_stacked_pr_pins_merge_base_and_persists_provenance(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    identity = GitIdentity(name="test", email="test@example.com")
    divergent_base_oid = commit_tree(
        scratch.path,
        scratch.base_tree_oid,
        "Divergent target tip\n",
        parents=(scratch.base_oid,),
        author=identity,
        committer=identity,
    )
    target = _target(scratch, base_ref_oid=divergent_base_oid)

    result = init_session_from_pr(scratch.path, target, slice_pr_prefix="review")
    stored = JsonSessionStore(scratch.path).load("main")

    assert result.session.base_ref == "target"
    assert result.session.base_oid == scratch.base_oid
    assert result.session.integration_pr == 73
    assert stored.base_ref == "target"
    assert stored.base_oid == scratch.base_oid
    assert stored.integration_pr == 73
    assert stored.source_pr is not None
    assert stored.source_pr.number == 73
    assert stored.source_pr.base_ref_oid == divergent_base_oid
    assert stored.source_pr.merge_base_oid == scratch.base_oid
    assert stored.slice_pr_prefix == "review"


@pytest.mark.integration
def test_publish_after_pr_init_targets_source_base_branch(
    scratch_repo_factory: ScratchRepoFactory,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    target = _target(scratch)
    remote = tmp_path / "remote.git"
    run_git(("init", "--quiet", "--bare", str(remote)), cwd=tmp_path)
    run_git(("remote", "add", "origin", str(remote)), cwd=scratch.path)
    fake_backend.seed(
        PRRecord(
            number=73,
            url=target.url,
            title="Existing integration",
            body="",
            state=PRState.OPEN,
            is_draft=True,
            base_ref="target",
            head_ref="main",
        )
    )
    init_session_from_pr(scratch.path, target)
    add_slice(scratch.path, "logic", "Logic")
    assign(scratch.path, "logic", ["app.py"])

    result = publish(scratch.path, backend=fake_backend)

    assert result.integration_pr == 73
    assert fake_backend.prs[73].base_ref == "target"
