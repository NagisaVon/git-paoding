"""Replay of Base-anchored atoms and construction of projection commits.

Both halves are deterministic: replay is a pure function of atom payloads, and
the synthetic commits fix identity and timestamps so an unchanged canonical
state reproduces byte-identical SHAs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from git_paoding.core.diffatoms import ReplayAtom
from git_paoding.core.model import AtomKind, PaodingError
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

_PROJECTION_IDENTITY = GitIdentity(
    name="git-paoding",
    email="git-paoding@localhost",
)


class ReplayError(PaodingError):
    """Raised when Base-anchored text atoms cannot be replayed safely."""


class ProjectionError(PaodingError):
    """Raised when a requested projection cannot form a valid Git tree."""


@dataclass(frozen=True, slots=True)
class ProjectionCommits:
    """Deterministic object IDs forming one slice's generated PR branches."""

    slice_id: str
    final_oid: str
    base_tree_oid: str
    head_tree_oid: str
    base_commit_oid: str
    head_commit_oid: str


@dataclass(slots=True)
class _TreeNode:
    entries: dict[str, _TreeNode | TreeEntry] = field(default_factory=dict)


def _base_index(replay_atom: ReplayAtom) -> int:
    atom = replay_atom.atom
    return atom.base_start if atom.base_len == 0 else atom.base_start - 1


def _application_key(replay_atom: ReplayAtom) -> tuple[int, int, int]:
    """Order edits for stable in-place splicing against Base coordinates.

    Higher Base positions run first. At the same list index, replacements run
    before insertions, and shared-gap insertions run in reverse ``gap_seq`` so
    repeated insertion at one index yields their ascending Final order.
    """

    atom = replay_atom.atom
    return (_base_index(replay_atom), int(atom.base_len > 0), atom.gap_seq)


def replay_file(
    base_content: bytes | None,
    replay_atoms: Sequence[ReplayAtom],
) -> bytes | None:
    """Replay a selected set of Base-anchored text atoms onto one Base file.

    ``None`` represents a missing file, allowing text add/delete atoms to use
    the same primitive. Whole-file atoms are intentionally rejected here:
    binary data, modes, and symlinks are applied by the tree/blob projection
    layer rather than pretending they are line-oriented edits.
    """

    if not replay_atoms:
        return base_content

    paths = {replay_atom.atom.path for replay_atom in replay_atoms}
    if len(paths) != 1:
        raise ReplayError("replay_file accepts atoms for exactly one path")

    whole_file_ids = [
        replay_atom.atom.atom_id
        for replay_atom in replay_atoms
        if replay_atom.atom.kind is AtomKind.WHOLE_FILE
    ]
    if whole_file_ids:
        joined_ids = ", ".join(whole_file_ids)
        raise ReplayError(f"whole-file atoms require tree/blob replay: {joined_ids}")

    if base_content is None:
        invalid = [
            replay_atom.atom.atom_id
            for replay_atom in replay_atoms
            if replay_atom.atom.kind is not AtomKind.ADD_FILE
        ]
        if invalid:
            raise ReplayError("only add-file atoms can be replayed onto a missing Base file")
        lines: list[bytes] = []
    else:
        lines = base_content.splitlines(keepends=True)

    replacement_indexes: set[int] = set()
    deletes_file = False
    creates_file = False
    for replay_atom in sorted(replay_atoms, key=_application_key, reverse=True):
        atom = replay_atom.atom
        index = _base_index(replay_atom)
        if index < 0 or index > len(lines):
            raise ReplayError(f"atom {atom.atom_id} has an out-of-range Base anchor")

        if atom.base_len > 0:
            if index in replacement_indexes:
                raise ReplayError(f"atoms overlap at Base index {index}")
            replacement_indexes.add(index)
            end = index + atom.base_len
            if end > len(lines):
                raise ReplayError(f"atom {atom.atom_id} extends past Base content")
            actual_removed = tuple(lines[index:end])
            if actual_removed != replay_atom.removed_lines:
                raise ReplayError(f"atom {atom.atom_id} does not match Base content")
            lines[index:end] = replay_atom.added_lines
        else:
            if replay_atom.removed_lines:
                raise ReplayError(f"insertion atom {atom.atom_id} unexpectedly removes content")
            lines[index:index] = replay_atom.added_lines

        deletes_file = deletes_file or atom.kind is AtomKind.DELETE_FILE
        creates_file = creates_file or atom.kind is AtomKind.ADD_FILE

    if deletes_file:
        if creates_file or lines:
            raise ReplayError("delete-file replay did not produce a missing file")
        return None
    return b"".join(lines)


def _load_tree(repo: Path, tree_oid: str) -> _TreeNode:
    node = _TreeNode()
    for entry in ls_tree(repo, tree_oid):
        if entry.object_type == "tree":
            node.entries[entry.path] = _load_tree(repo, entry.oid)
        else:
            node.entries[entry.path] = entry
    return node


def _path_parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/"))
    if not parts or any(not part for part in parts):
        raise ProjectionError(f"invalid Git path in atom: {path!r}")
    return parts


def _lookup_entry(root: _TreeNode, path: str) -> TreeEntry | None:
    node = root
    parts = _path_parts(path)
    for part in parts[:-1]:
        child = node.entries.get(part)
        if child is None:
            return None
        if isinstance(child, TreeEntry):
            # A file-to-directory (or reverse) transition makes the deeper
            # path absent on this side of the comparison.
            return None
        node = child
    value = node.entries.get(parts[-1])
    if isinstance(value, _TreeNode):
        return None
    return value


def _delete_path(root: _TreeNode, path: str) -> None:
    parts = _path_parts(path)

    def remove(node: _TreeNode, index: int) -> bool:
        part = parts[index]
        if index == len(parts) - 1:
            node.entries.pop(part, None)
            return not node.entries
        child = node.entries.get(part)
        if child is None:
            return not node.entries
        if isinstance(child, TreeEntry):
            # Removing a deeper path beneath a file is already satisfied.  A
            # desired replacement will create the needed directory later.
            return not node.entries
        if remove(child, index + 1):
            node.entries.pop(part, None)
        return not node.entries

    remove(root, 0)


def _set_path(root: _TreeNode, path: str, entry: TreeEntry) -> None:
    node = root
    parts = _path_parts(path)
    for part in parts[:-1]:
        child = node.entries.get(part)
        if child is None:
            child = _TreeNode()
            node.entries[part] = child
        elif isinstance(child, TreeEntry):
            raise ProjectionError(
                f"cannot create {path!r}: its parent component {part!r} is a file"
            )
        node = child
    existing = node.entries.get(parts[-1])
    if isinstance(existing, _TreeNode):
        raise ProjectionError(f"cannot replace tree {path!r} with a file in one projection")
    node.entries[parts[-1]] = TreeEntry(
        mode=entry.mode,
        object_type=entry.object_type,
        oid=entry.oid,
        path=parts[-1],
    )


def _write_tree(repo: Path, node: _TreeNode) -> str:
    entries: list[TreeEntry] = []
    for name, value in node.entries.items():
        if isinstance(value, _TreeNode):
            entries.append(
                TreeEntry(
                    mode="040000",
                    object_type="tree",
                    oid=_write_tree(repo, value),
                    path=name,
                )
            )
        else:
            entries.append(value)
    return mktree(repo, entries)


def _entry_content(repo: Path, entry: TreeEntry | None, *, path: str) -> bytes | None:
    if entry is None:
        return None
    if entry.object_type != "blob":
        raise ProjectionError(f"text atom path {path!r} does not resolve to a blob")
    return cat_file(repo, entry.oid)


def _synthetic_entry(
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
        if len(path_atoms) != 1 or len(whole_file_atoms) != 1:
            raise ProjectionError(f"whole-file path {path!r} must have exactly one atom")
        # This function is called only for paths touched by the requested
        # slice, so removing that sole whole-file atom restores Base exactly.
        return base_entry

    non_slice_atoms = tuple(item for item in path_atoms if item.atom.owner != slice_id)
    content = replay_file(
        _entry_content(repo, base_entry, path=path),
        non_slice_atoms,
    )
    if content is None:
        return None

    mode_source = final_entry or base_entry
    if mode_source is None or mode_source.object_type != "blob":
        raise ProjectionError(f"could not determine blob mode for projected path {path!r}")
    return TreeEntry(
        mode=mode_source.mode,
        object_type="blob",
        oid=hash_object(repo, content),
        path=_path_parts(path)[-1],
    )


def build_projection(
    repo: Path,
    *,
    base_oid: str,
    final_oid: str,
    slice_id: str,
    replay_atoms: Sequence[ReplayAtom],
) -> ProjectionCommits:
    """Build deterministic full-Final-tree projection commits for one slice.

    The synthetic base starts from the complete Final tree and replaces only
    files containing ``slice_id`` atoms with Base plus all non-slice atoms.
    The generated head always uses the untouched full Final tree.  All objects
    are written through Git plumbing; HEAD, the index, and the worktree are not
    consulted or modified.
    """

    if not slice_id:
        raise ProjectionError("slice id must not be empty")

    base_tree_oid = rev_parse(repo, f"{base_oid}^{{tree}}")
    final_tree_oid = rev_parse(repo, f"{final_oid}^{{tree}}")
    base_root = _load_tree(repo, base_tree_oid)
    synthetic_root = _load_tree(repo, final_tree_oid)

    atoms_by_path: dict[str, list[ReplayAtom]] = {}
    for replay_atom in replay_atoms:
        atoms_by_path.setdefault(replay_atom.atom.path, []).append(replay_atom)

    desired_entries: dict[str, TreeEntry | None] = {}
    for path, path_atoms in atoms_by_path.items():
        if not any(item.atom.owner == slice_id for item in path_atoms):
            continue
        desired_entries[path] = _synthetic_entry(
            repo,
            path=path,
            slice_id=slice_id,
            path_atoms=path_atoms,
            base_entry=_lookup_entry(base_root, path),
            final_entry=_lookup_entry(synthetic_root, path),
        )

    # Clear every touched path before materializing replacements, so a path
    # can swap between file and directory without colliding with its old entry.
    for path in sorted(desired_entries, key=lambda value: value.count("/"), reverse=True):
        _delete_path(synthetic_root, path)
    for path in sorted(desired_entries, key=lambda value: value.count("/")):
        entry = desired_entries[path]
        if entry is not None:
            _set_path(synthetic_root, path, entry)

    synthetic_tree_oid = _write_tree(repo, synthetic_root)
    final_date = commit_committer_date(repo, final_oid)
    identity = GitIdentity(
        name=_PROJECTION_IDENTITY.name,
        email=_PROJECTION_IDENTITY.email,
        date=final_date,
    )
    base_message = f"git-paoding projection base\nslice: {slice_id}\nfinal: {final_oid}\n"
    base_commit_oid = commit_tree(
        repo,
        synthetic_tree_oid,
        base_message,
        parents=(base_oid,),
        author=identity,
        committer=identity,
    )
    head_message = f"git-paoding projection head\nslice: {slice_id}\nfinal: {final_oid}\n"
    head_commit_oid = commit_tree(
        repo,
        final_tree_oid,
        head_message,
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
