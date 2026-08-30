"""Integration coverage for deterministic full-Final-tree projections."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import RepoFile, ScratchRepoFactory
from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.model import AtomState
from git_paoding.core.projection import ProjectionCommits, build_projection
from git_paoding.gitio.diffparse import RawDiffHunk, diff_trees
from git_paoding.gitio.runner import run_git


def _own(replay_atom: ReplayAtom, owner: str) -> ReplayAtom:
    return replace(
        replay_atom,
        atom=replay_atom.atom.model_copy(update={"owner": owner, "state": AtomState.ASSIGNED}),
    )


def _payloads(
    atoms: tuple[ReplayAtom, ...],
) -> tuple[tuple[str, str, tuple[bytes, ...], tuple[bytes, ...], str | None], ...]:
    return tuple(
        (
            item.atom.path,
            item.atom.kind.value,
            item.removed_lines,
            item.added_lines,
            item.atom.content_hash if item.atom.kind.value == "whole-file" else None,
        )
        for item in atoms
    )


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


def _tree_paths(repo_path: Path, treeish: str) -> set[str]:
    output = run_git(("ls-tree", "-r", "--name-only", "-z", treeish), cwd=repo_path).stdout
    return {item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item}


@pytest.mark.integration
def test_build_twice_is_byte_identical_and_same_file_slices_are_isolated(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"shared.txt": "alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\ngolf\n"},
        {"shared.txt": "ALPHA\nbravo\nCHARLIE\ndelta\nECHO\nfoxtrot\nGOLF\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    assert len(raw_atoms) == 4
    owned_atoms = (
        _own(raw_atoms[0], "slice-a"),
        _own(raw_atoms[1], "slice-b"),
        _own(raw_atoms[2], "slice-c"),
        _own(raw_atoms[3], "slice-a"),
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
    projection_c = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-c",
        replay_atoms=owned_atoms,
    )

    assert second_a == first_a
    assert first_a.head_tree_oid == repo.final_tree_oid
    assert projection_b.head_tree_oid == repo.final_tree_oid
    assert projection_c.head_tree_oid == repo.final_tree_oid
    _assert_projection_shape(repo.path, first_a, (owned_atoms[0], owned_atoms[3]))
    _assert_projection_shape(repo.path, projection_b, (owned_atoms[1],))
    _assert_projection_shape(repo.path, projection_c, (owned_atoms[2],))


@pytest.mark.integration
def test_projection_handles_owned_and_context_file_creates_and_deletes(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {
            "owned-deleted.txt": "owned old\n",
            "context-deleted.txt": "context old\n",
            "context.txt": "unchanged\n",
        },
        {
            "owned-created.txt": "owned new\n",
            "context-created.txt": "context new\n",
            "context.txt": "unchanged\n",
        },
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owner_by_path = {
        "owned-deleted.txt": "slice-a",
        "owned-created.txt": "slice-a",
        "context-deleted.txt": "slice-b",
        "context-created.txt": "slice-b",
    }
    owned_atoms = tuple(_own(item, owner_by_path[item.atom.path]) for item in raw_atoms)

    projection_a = build_projection(
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

    expected_a = tuple(item for item in owned_atoms if item.atom.owner == "slice-a")
    expected_b = tuple(item for item in owned_atoms if item.atom.owner == "slice-b")
    _assert_projection_shape(repo.path, projection_a, expected_a)
    _assert_projection_shape(repo.path, projection_b, expected_b)

    assert _tree_paths(repo.path, projection_a.base_commit_oid) == {
        "context-created.txt",
        "context.txt",
        "owned-deleted.txt",
    }
    assert _tree_paths(repo.path, projection_b.base_commit_oid) == {
        "context-deleted.txt",
        "context.txt",
        "owned-created.txt",
    }


@pytest.mark.integration
def test_projection_handles_owned_and_context_whole_file_atoms(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {
            "owned.bin": b"\x00owned-before",
            "context.bin": b"\x00context-before",
            "owned-mode.sh": RepoFile("echo owned\n"),
            "context-mode.sh": RepoFile("echo context\n"),
            "owned-link": RepoFile("owned-before", symlink=True),
            "context-link": RepoFile("context-before", symlink=True),
        },
        {
            "owned.bin": b"\x00owned-after",
            "context.bin": b"\x00context-after",
            "owned-mode.sh": RepoFile("echo owned\n", executable=True),
            "context-mode.sh": RepoFile("echo context\n", executable=True),
            "owned-link": RepoFile("owned-after", symlink=True),
            "context-link": RepoFile("context-after", symlink=True),
        },
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    assert {item.atom.path for item in raw_atoms} == {
        "owned.bin",
        "context.bin",
        "owned-mode.sh",
        "context-mode.sh",
        "owned-link",
        "context-link",
    }
    owned_atoms = tuple(
        _own(item, "slice-a" if item.atom.path.startswith("owned") else "slice-b")
        for item in raw_atoms
    )

    projection_a = build_projection(
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

    _assert_projection_shape(
        repo.path,
        projection_a,
        tuple(item for item in owned_atoms if item.atom.owner == "slice-a"),
    )
    _assert_projection_shape(
        repo.path,
        projection_b,
        tuple(item for item in owned_atoms if item.atom.owner == "slice-b"),
    )


@pytest.mark.integration
def test_projection_preserves_shared_gap_sequence_across_slices(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"shared.txt": "anchor\n"},
        {"shared.txt": "a-first\nb-middle\nc-middle\na-last\nanchor\n"},
    )
    gap_hunks = (
        RawDiffHunk(
            path="shared.txt",
            base_start=0,
            base_len=0,
            final_start=1,
            final_len=1,
            removed_lines=(),
            added_lines=("a-first\n",),
        ),
        RawDiffHunk(
            path="shared.txt",
            base_start=0,
            base_len=0,
            final_start=2,
            final_len=1,
            removed_lines=(),
            added_lines=("b-middle\n",),
        ),
        RawDiffHunk(
            path="shared.txt",
            base_start=0,
            base_len=0,
            final_start=3,
            final_len=1,
            removed_lines=(),
            added_lines=("c-middle\n",),
        ),
        RawDiffHunk(
            path="shared.txt",
            base_start=0,
            base_len=0,
            final_start=4,
            final_len=1,
            removed_lines=(),
            added_lines=("a-last\n",),
        ),
    )
    gap_atoms = atomize_hunks(gap_hunks)
    assert [item.atom.gap_seq for item in gap_atoms] == [0, 1, 2, 3]
    owned_atoms = (
        _own(gap_atoms[0], "slice-a"),
        _own(gap_atoms[1], "slice-b"),
        _own(gap_atoms[2], "slice-c"),
        _own(gap_atoms[3], "slice-a"),
    )

    projection_a = build_projection(
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
    projection_c = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-c",
        replay_atoms=owned_atoms,
    )

    _assert_projection_shape(repo.path, projection_a, (owned_atoms[0], owned_atoms[3]))
    _assert_projection_shape(repo.path, projection_b, (owned_atoms[1],))
    _assert_projection_shape(repo.path, projection_c, (owned_atoms[2],))


@pytest.mark.integration
def test_projection_commit_identity_ignores_process_identity_locale_and_timezone(
    scratch_repo_factory: ScratchRepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = scratch_repo_factory({"a.txt": "before\n"}, {"a.txt": "after\n"})
    atom = _own(atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))[0], "slice-a")
    first = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=(atom,),
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ambient author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ambient-author@example.test")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2038-01-01T00:00:00-12:00")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "ambient committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ambient-committer@example.test")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2038-01-01T00:00:00+14:00")
    monkeypatch.setenv("LANG", "tr_TR.UTF-8")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")

    second = build_projection(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=(atom,),
    )

    assert second == first


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
