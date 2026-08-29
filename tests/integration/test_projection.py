"""Integration coverage for deterministic full-Final-tree projections."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.model import AtomState
from git_paoding.core.projection import ProjectionCommits, build_projection
from git_paoding.gitio.diffparse import diff_trees
from git_paoding.gitio.runner import run_git


def _own(replay_atom: ReplayAtom, owner: str) -> ReplayAtom:
    return replace(
        replay_atom,
        atom=replay_atom.atom.model_copy(update={"owner": owner, "state": AtomState.ASSIGNED}),
    )


def _payloads(
    atoms: tuple[ReplayAtom, ...],
) -> tuple[tuple[str, tuple[bytes, ...], tuple[bytes, ...]], ...]:
    return tuple((item.atom.path, item.removed_lines, item.added_lines) for item in atoms)


def _assert_projection_shape(
    repo_path: Path,
    projection: ProjectionCommits,
    expected_atoms: tuple[ReplayAtom, ...],
) -> None:
    # ``Path`` is kept out of this helper's public fixture-oriented call sites;
    # run_git/build helpers accept the concrete Path supplied by the fixture.
    merge_base = (
        run_git(
            ("merge-base", projection.head_commit_oid, projection.base_commit_oid),
            cwd=repo_path,
        )
        .stdout_text()
        .strip()
    )
    assert merge_base == projection.base_commit_oid
    projected_atoms = atomize_hunks(
        diff_trees(
            repo_path,
            projection.base_commit_oid,
            projection.head_commit_oid,
        )
    )
    assert _payloads(projected_atoms) == _payloads(expected_atoms)


@pytest.mark.integration
def test_build_twice_is_byte_identical_and_same_file_slices_are_isolated(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"shared.txt": "alpha\nbravo\ncharlie\ndelta\necho\n"},
        {"shared.txt": "ALPHA\nbravo\nCHARLIE\ndelta\nECHO\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    assert len(raw_atoms) == 3
    owned_atoms = (
        _own(raw_atoms[0], "slice-a"),
        _own(raw_atoms[1], "slice-b"),
        _own(raw_atoms[2], "slice-a"),
    )

    first_a = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    second_a = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    projection_b = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-b",
        replay_atoms=owned_atoms,
    )

    assert second_a == first_a
    assert first_a.head_tree_oid == repo.final_tree_oid
    assert projection_b.head_tree_oid == repo.final_tree_oid
    _assert_projection_shape(repo.path, first_a, (owned_atoms[0], owned_atoms[2]))
    _assert_projection_shape(repo.path, projection_b, (owned_atoms[1],))


@pytest.mark.integration
def test_projection_handles_slice_creates_deletes_and_whole_file_atoms(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {
            "nested/deleted.txt": "old\n",
            "binary.bin": b"\x00before",
            "context.txt": "unchanged\n",
        },
        {
            "nested/created.txt": "new\n",
            "binary.bin": b"\x00after",
            "context.txt": "unchanged\n",
        },
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owner_by_path = {
        "nested/deleted.txt": "slice-text",
        "nested/created.txt": "slice-text",
        "binary.bin": "slice-binary",
    }
    owned_atoms = tuple(_own(item, owner_by_path[item.atom.path]) for item in raw_atoms)

    text_projection = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-text",
        replay_atoms=owned_atoms,
    )
    binary_projection = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-binary",
        replay_atoms=owned_atoms,
    )

    text_paths = {
        hunk.path
        for hunk in diff_trees(
            repo.path,
            text_projection.base_commit_oid,
            text_projection.head_commit_oid,
        )
    }
    binary_paths = {
        hunk.path
        for hunk in diff_trees(
            repo.path,
            binary_projection.base_commit_oid,
            binary_projection.head_commit_oid,
        )
    }
    assert text_paths == {"nested/created.txt", "nested/deleted.txt"}
    assert binary_paths == {"binary.bin"}


@pytest.mark.integration
def test_projection_handles_file_to_directory_replacement_in_one_slice(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"node": "base file\n"},
        {"node/child.txt": "final child\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owned_atoms = tuple(_own(item, "slice-shape") for item in raw_atoms)

    projection = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-shape",
        replay_atoms=owned_atoms,
    )

    assert {
        hunk.path
        for hunk in diff_trees(
            repo.path,
            projection.base_commit_oid,
            projection.head_commit_oid,
        )
    } == {"node", "node/child.txt"}
