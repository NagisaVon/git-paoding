"""Architectural operation-count acceptance on a field-shaped repository."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

import git_paoding.gitio.plumbing as plumbing_module
import git_paoding.gitio.refs as refs_module
from conftest import FakeBackend, ScratchRepoFactory
from git_paoding.api import add_slice, assign_batch, get_status, init_session, publish
from git_paoding.core.model import AssignBatchRequest, PublishOutcome
from git_paoding.gitio.runner import GitResult, run_git
from git_paoding.gitio.trace import OpCategory, collecting

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from scripts.field_shape import (  # noqa: E402
    ATOM_COUNT,
    CHANGED_FILE_COUNT,
    DIRECTORY_COUNT,
    SLICE_COUNT,
    build_field_shape,
)

pytestmark = pytest.mark.performance


def _prepare_field_repository(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    backend: FakeBackend,
) -> tuple[Path, int]:
    shape = build_field_shape()
    scratch = scratch_repo_factory(shape.base, shape.final)
    remote = tmp_path / "field-shaped.git"
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    run_git(("init", "--bare", "--quiet", str(remote)), cwd=tmp_path)
    run_git(("remote", "add", "origin", str(remote)), cwd=scratch.path)
    run_git(
        (
            "push",
            "--quiet",
            "origin",
            "refs/heads/base:refs/heads/base",
            "refs/heads/main:refs/heads/main",
        ),
        cwd=scratch.path,
    )
    init_session(scratch.path, "base")
    for slice_id in shape.slice_paths:
        add_slice(scratch.path, slice_id, f"Field-shaped review {slice_id}")
    assign_batch(
        scratch.path,
        AssignBatchRequest(
            assignments={slice_id: list(paths) for slice_id, paths in shape.slice_paths.items()}
        ),
    )

    status = get_status(scratch.path)
    assert len(status.atoms) == ATOM_COUNT
    assert len({atom.path for atom in status.atoms}) == CHANGED_FILE_COUNT
    assert len(status.slices) == SLICE_COUNT
    recursive_entries = plumbing_module.ls_tree_recursive(scratch.path, scratch.final_tree_oid)
    assert sum(entry.object_type == "tree" for entry in recursive_entries) >= DIRECTORY_COUNT
    return scratch.path, shape.dirty_ancestor_bound


def test_publish_architectural_operation_counts(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, dirty_ancestor_bound = _prepare_field_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
    )
    commands: list[tuple[str, ...]] = []
    real_run_git = run_git

    def traced_run_git(
        args: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        commands.append(tuple(args))
        return real_run_git(
            args,
            cwd=cwd,
            input_data=input_data,
            env=env,
            timeout=timeout,
        )

    monkeypatch.setattr(plumbing_module, "run_git", traced_run_git)
    monkeypatch.setattr(refs_module, "run_git", traced_run_git)
    with collecting() as trace:
        result = publish(repo, backend=fake_backend)

    recursive_tree_reads = sum(
        command[:5] == ("ls-tree", "-r", "-t", "-z", "--full-tree") for command in commands
    )
    mktree_walks = sum(command[:1] == ("mktree",) for command in commands)
    ls_remote_calls = sum(command[:1] == ("ls-remote",) for command in commands)
    push_calls = sum(command[:1] == ("push",) for command in commands)
    first_mutation = next(
        index
        for index, operation in enumerate(fake_backend.call_log)
        if operation.startswith(("create:", "update:", "close:"))
    )
    github_mutation_readbacks = sum(
        operation == "list_open_prs" or operation.startswith("get:")
        for operation in fake_backend.call_log[first_mutation + 1 :]
    )

    print(
        "field-shape metrics: "
        f"directories={DIRECTORY_COUNT} changed_files={CHANGED_FILE_COUNT} "
        f"atoms={ATOM_COUNT} slices={SLICE_COUNT} recursive_tree_reads={recursive_tree_reads} "
        f"mktree={mktree_walks}/{dirty_ancestor_bound} ls_remote={ls_remote_calls} "
        f"push={push_calls} github_mutation_readbacks={github_mutation_readbacks}"
    )
    assert [item.outcome for item in result.slices] == [PublishOutcome.CREATED] * SLICE_COUNT
    assert recursive_tree_reads == 2
    assert mktree_walks <= dirty_ancestor_bound
    assert ls_remote_calls == 1
    assert push_calls <= 1
    assert github_mutation_readbacks == 0
    assert trace.counts[OpCategory.GIT_REMOTE] == ls_remote_calls + push_calls
    assert trace.counts[OpCategory.GH_READ] == 0
