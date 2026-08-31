"""Integration coverage for deterministic full-Final-tree projections."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import RepoFile, ScratchRepoFactory
from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.model import AtomKind, AtomState
from git_paoding.core.projection import (
    ProjectionCommits,
    build_projection,
    build_projections,
    load_projection_context,
    replay_file,
)
from git_paoding.gitio.diffparse import RawDiffHunk, diff_trees
from git_paoding.gitio.plumbing import (
    GitIdentity,
    TreeEntry,
    cat_file,
    commit_committer_date,
    commit_tree,
    hash_object,
    ls_tree,
    mktree,
    rev_parse,
)
from git_paoding.gitio.runner import run_git
from git_paoding.gitio.trace import OpCategory, collecting


@dataclass(slots=True)
class _ReferenceTreeNode:
    entries: dict[str, _ReferenceTreeNode | TreeEntry] = field(default_factory=dict)


def _reference_load_tree(repo: Path, tree_oid: str) -> _ReferenceTreeNode:
    node = _ReferenceTreeNode()
    for entry in ls_tree(repo, tree_oid):
        if entry.object_type == "tree":
            node.entries[entry.path] = _reference_load_tree(repo, entry.oid)
        else:
            node.entries[entry.path] = entry
    return node


def _reference_parts(path: str) -> tuple[str, ...]:
    return tuple(path.split("/"))


def _reference_lookup(root: _ReferenceTreeNode, path: str) -> TreeEntry | None:
    node = root
    parts = _reference_parts(path)
    for part in parts[:-1]:
        child = node.entries.get(part)
        if child is None or isinstance(child, TreeEntry):
            return None
        node = child
    value = node.entries.get(parts[-1])
    return None if isinstance(value, _ReferenceTreeNode) else value


def _reference_delete(root: _ReferenceTreeNode, path: str) -> None:
    parts = _reference_parts(path)

    def remove(node: _ReferenceTreeNode, index: int) -> bool:
        part = parts[index]
        if index == len(parts) - 1:
            node.entries.pop(part, None)
            return not node.entries
        child = node.entries.get(part)
        if child is None or isinstance(child, TreeEntry):
            return not node.entries
        if remove(child, index + 1):
            node.entries.pop(part, None)
        return not node.entries

    remove(root, 0)


def _reference_set(root: _ReferenceTreeNode, path: str, entry: TreeEntry) -> None:
    node = root
    parts = _reference_parts(path)
    for part in parts[:-1]:
        child = node.entries.get(part)
        if child is None:
            child = _ReferenceTreeNode()
            node.entries[part] = child
        elif isinstance(child, TreeEntry):
            raise AssertionError(f"reference builder found file parent in {path!r}")
        node = child
    if isinstance(node.entries.get(parts[-1]), _ReferenceTreeNode):
        raise AssertionError(f"reference builder found tree replacement in {path!r}")
    node.entries[parts[-1]] = TreeEntry(
        mode=entry.mode,
        object_type=entry.object_type,
        oid=entry.oid,
        path=parts[-1],
    )


def _reference_write_tree(repo: Path, node: _ReferenceTreeNode) -> str:
    entries: list[TreeEntry] = []
    for name, value in node.entries.items():
        if isinstance(value, _ReferenceTreeNode):
            entries.append(
                TreeEntry(
                    mode="040000",
                    object_type="tree",
                    oid=_reference_write_tree(repo, value),
                    path=name,
                )
            )
        else:
            entries.append(value)
    return mktree(repo, entries)


def _reference_content(repo: Path, entry: TreeEntry | None, path: str) -> bytes | None:
    if entry is None:
        return None
    if entry.object_type != "blob":
        raise AssertionError(f"reference text path {path!r} is not a blob")
    return cat_file(repo, entry.oid)


def _reference_synthetic_entry(
    repo: Path,
    *,
    path: str,
    slice_id: str,
    path_atoms: Sequence[ReplayAtom],
    base_entry: TreeEntry | None,
    final_entry: TreeEntry | None,
) -> TreeEntry | None:
    whole_file_atoms = [item for item in path_atoms if item.atom.kind is AtomKind.WHOLE_FILE]
    if whole_file_atoms:
        return base_entry
    content = replay_file(
        _reference_content(repo, base_entry, path),
        tuple(item for item in path_atoms if item.atom.owner != slice_id),
    )
    if content is None:
        return None
    mode_source = final_entry or base_entry
    if mode_source is None or mode_source.object_type != "blob":
        raise AssertionError(f"reference builder has no mode for {path!r}")
    return TreeEntry(
        mode=mode_source.mode,
        object_type="blob",
        oid=hash_object(repo, content),
        path=path.rsplit("/", maxsplit=1)[-1],
    )


def _reference_build_projection(
    repo: Path,
    *,
    base_oid: str,
    final_oid: str,
    slice_id: str,
    replay_atoms: Sequence[ReplayAtom],
) -> ProjectionCommits:
    base_tree_oid = rev_parse(repo, f"{base_oid}^{{tree}}")
    final_tree_oid = rev_parse(repo, f"{final_oid}^{{tree}}")
    base_root = _reference_load_tree(repo, base_tree_oid)
    synthetic_root = _reference_load_tree(repo, final_tree_oid)
    atoms_by_path: dict[str, list[ReplayAtom]] = {}
    for replay_atom in replay_atoms:
        atoms_by_path.setdefault(replay_atom.atom.path, []).append(replay_atom)
    desired: dict[str, TreeEntry | None] = {}
    for path, path_atoms in atoms_by_path.items():
        if any(item.atom.owner == slice_id for item in path_atoms):
            desired[path] = _reference_synthetic_entry(
                repo,
                path=path,
                slice_id=slice_id,
                path_atoms=path_atoms,
                base_entry=_reference_lookup(base_root, path),
                final_entry=_reference_lookup(synthetic_root, path),
            )
    for path in sorted(desired, key=lambda value: value.count("/"), reverse=True):
        _reference_delete(synthetic_root, path)
    for path in sorted(desired, key=lambda value: value.count("/")):
        entry = desired[path]
        if entry is not None:
            _reference_set(synthetic_root, path, entry)

    synthetic_tree_oid = _reference_write_tree(repo, synthetic_root)
    identity = GitIdentity(
        name="git-paoding",
        email="git-paoding@localhost",
        date=commit_committer_date(repo, final_oid),
    )
    base_commit_oid = commit_tree(
        repo,
        synthetic_tree_oid,
        f"git-paoding projection base\nslice: {slice_id}\nfinal: {final_oid}\n",
        parents=(base_oid,),
        author=identity,
        committer=identity,
    )
    head_commit_oid = commit_tree(
        repo,
        final_tree_oid,
        f"git-paoding projection head\nslice: {slice_id}\nfinal: {final_oid}\n",
        parents=(base_commit_oid,),
        author=identity,
        committer=identity,
    )
    return ProjectionCommits(
        slice_id=slice_id,
        final_oid=final_oid,
        base_tree_oid=synthetic_tree_oid,
        head_tree_oid=final_tree_oid,
        base_commit_oid=base_commit_oid,
        head_commit_oid=head_commit_oid,
    )


def _build_equivalent(
    repo: Path,
    *,
    base_oid: str,
    final_oid: str,
    slice_id: str,
    replay_atoms: Sequence[ReplayAtom],
) -> ProjectionCommits:
    reference = _reference_build_projection(
        repo,
        base_oid=base_oid,
        final_oid=final_oid,
        slice_id=slice_id,
        replay_atoms=replay_atoms,
    )
    projection = build_projection(
        repo,
        base_oid=base_oid,
        final_oid=final_oid,
        slice_id=slice_id,
        replay_atoms=replay_atoms,
    )
    assert projection.base_tree_oid == reference.base_tree_oid
    assert projection.head_tree_oid == reference.head_tree_oid
    assert projection.base_commit_oid == reference.base_commit_oid
    assert projection.head_commit_oid == reference.head_commit_oid
    return projection


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

    first_a = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    second_a = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    projection_b = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-b",
        replay_atoms=owned_atoms,
    )
    projection_c = _build_equivalent(
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

    projection_a = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    projection_b = _build_equivalent(
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

    projection_a = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    projection_b = _build_equivalent(
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

    projection_a = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-a",
        replay_atoms=owned_atoms,
    )
    projection_b = _build_equivalent(
        repo.path,
        base_oid=repo.base_oid,
        final_oid=repo.final_oid,
        slice_id="slice-b",
        replay_atoms=owned_atoms,
    )
    projection_c = _build_equivalent(
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
    first = _build_equivalent(
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

    second = _build_equivalent(
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

    projection = _build_equivalent(
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


@pytest.mark.integration
def test_projection_handles_directory_to_file_replacement_in_one_slice(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"node/child.txt": "base child\n"},
        {"node": "final file\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owned_atoms = tuple(_own(item, "slice-shape") for item in raw_atoms)

    projection = _build_equivalent(
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


@pytest.mark.integration
def test_projection_preserves_empty_existing_slice_and_non_utf_8_path(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    unusual_path = "nested/non-utf-8-\udcff.txt"
    repo = scratch_repo_factory({"anchor.txt": "base\n"}, {"anchor.txt": "final\n"})
    before_oid = hash_object(repo.path, b"before\n")
    after_oid = hash_object(repo.path, b"after\n")
    nested_base_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="100644",
                object_type="blob",
                oid=before_oid,
                path=unusual_path.rsplit("/", maxsplit=1)[-1],
            ),
        ),
    )
    nested_final_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="100644",
                object_type="blob",
                oid=after_oid,
                path=unusual_path.rsplit("/", maxsplit=1)[-1],
            ),
        ),
    )
    base_tree_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="040000",
                object_type="tree",
                oid=nested_base_oid,
                path="nested",
            ),
        ),
    )
    final_tree_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="040000",
                object_type="tree",
                oid=nested_final_oid,
                path="nested",
            ),
        ),
    )
    identity = GitIdentity(
        name="git-paoding tests",
        email="git-paoding@localhost",
        date="2001-01-01T00:00:00+00:00",
    )
    base_oid = commit_tree(
        repo.path,
        base_tree_oid,
        "Non-UTF-8 Base\n",
        author=identity,
        committer=identity,
    )
    final_identity = GitIdentity(
        name=identity.name,
        email=identity.email,
        date="2001-01-01T00:00:01+00:00",
    )
    final_oid = commit_tree(
        repo.path,
        final_tree_oid,
        "Non-UTF-8 Final\n",
        parents=(base_oid,),
        author=final_identity,
        committer=final_identity,
    )
    unusual_hunk = diff_trees(repo.path, base_oid, final_oid)[0]
    safe_hunk = replace(unusual_hunk, path="safe-placeholder.txt")
    safe_atom = atomize_hunks((safe_hunk,))[0]
    unusual_atom = replace(
        safe_atom,
        atom=safe_atom.atom.model_copy(update={"path": unusual_path}),
    )
    atom = _own(unusual_atom, "slice-a")

    projection = _build_equivalent(
        repo.path,
        base_oid=base_oid,
        final_oid=final_oid,
        slice_id="empty-existing-slice",
        replay_atoms=(atom,),
    )

    assert projection.base_tree_oid == final_tree_oid
    assert projection.head_tree_oid == final_tree_oid


@pytest.mark.integration
@pytest.mark.parametrize("seed", range(8))
def test_randomized_repositories_match_reference_builder(
    scratch_repo_factory: ScratchRepoFactory,
    seed: int,
) -> None:
    generator = random.Random(seed)
    base: dict[str, str | bytes | RepoFile] = {}
    final: dict[str, str | bytes | RepoFile] = {}
    for index in range(10):
        path = f"group-{index % 3}/file-{index}.txt"
        lines = [f"{index}-{line}\n" for line in range(4)]
        base[path] = "".join(lines)
        action = generator.choice(("modify", "modify", "delete", "keep"))
        if action == "modify":
            changed = list(lines)
            changed[generator.randrange(len(changed))] = f"changed-{seed}-{index}\n"
            final[path] = "".join(changed)
        elif action == "keep":
            final[path] = base[path]
    for index in range(3):
        final[f"added-{seed}/new-{index}.txt"] = f"new-{generator.randrange(1000)}\n"

    repo = scratch_repo_factory(base, final)
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owners = ("slice-a", "slice-b", "slice-c")
    owned_atoms = tuple(_own(item, generator.choice(owners)) for item in raw_atoms)

    for slice_id in owners:
        _build_equivalent(
            repo.path,
            base_oid=repo.base_oid,
            final_oid=repo.final_oid,
            slice_id=slice_id,
            replay_atoms=owned_atoms,
        )


@pytest.mark.integration
def test_multi_slice_context_uses_two_recursive_tree_reads_and_no_per_slice_reads(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"nested/shared.txt": "one\ntwo\nthree\nfour\nfive\n"},
        {"nested/shared.txt": "ONE\ntwo\nTHREE\nfour\nFIVE\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    owned_atoms = tuple(
        _own(item, ("slice-a", "slice-b", "slice-c")[index]) for index, item in enumerate(raw_atoms)
    )

    with patch("git_paoding.gitio.plumbing.run_git", wraps=run_git) as git_call:
        with collecting() as trace:
            context = load_projection_context(
                repo.path,
                base_oid=repo.base_oid,
                final_oid=repo.final_oid,
                replay_atoms=owned_atoms,
            )
            projections = build_projections(context, ("slice-a", "slice-b", "slice-c"))

    commands = [tuple(call.args[0]) for call in git_call.call_args_list]
    recursive_reads = [
        command
        for command in commands
        if command[:5] == ("ls-tree", "-r", "-t", "-z", "--full-tree")
    ]
    direct_reads = [
        command
        for command in commands
        if command and command[0] == "ls-tree" and "-r" not in command
    ]
    revision_reads = [command for command in commands if command and command[0] == "rev-parse"]
    committer_date_reads = [
        command for command in commands if command[:2] == ("show", "--no-patch")
    ]
    assert len(revision_reads) == 2
    assert len(committer_date_reads) == 1
    assert len(recursive_reads) == 2
    assert direct_reads == []
    assert trace.counts[OpCategory.GIT_LOCAL] == len(commands)
    assert set(projections) == {"slice-a", "slice-b", "slice-c"}

    for slice_id, projection in projections.items():
        reference = _reference_build_projection(
            repo.path,
            base_oid=repo.base_oid,
            final_oid=repo.final_oid,
            slice_id=slice_id,
            replay_atoms=owned_atoms,
        )
        assert projection.base_tree_oid == reference.base_tree_oid
        assert projection.head_tree_oid == reference.head_tree_oid
        assert projection.base_commit_oid == reference.base_commit_oid
        assert projection.head_commit_oid == reference.head_commit_oid
