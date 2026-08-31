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
    ls_tree_recursive,
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


@dataclass(frozen=True, slots=True)
class _FrozenTree:
    """An immutable snapshot node retaining its existing Git tree object."""

    oid: str
    entries: dict[str, _FrozenTree | TreeEntry]


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    """Repository snapshot and replay indexes shared by projection builds."""

    repo: Path
    base_oid: str
    final_oid: str
    base_tree_oid: str
    final_tree_oid: str
    final_committer_date: str
    final_root: _FrozenTree
    base_entries: dict[str, TreeEntry]
    atoms_by_path: dict[str, tuple[ReplayAtom, ...]]
    _blob_cache: dict[str, bytes]


@dataclass(slots=True)
class _CowTree:
    """Sparse changes layered over one frozen tree node."""

    frozen: _FrozenTree | None
    changes: dict[str, _CowTree | TreeEntry | None] = field(default_factory=dict)


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


def _path_parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/"))
    if not parts or any(not part for part in parts):
        raise ProjectionError(f"invalid Git path in atom: {path!r}")
    return parts


def _fold_tree(tree_oid: str, entries: Sequence[TreeEntry]) -> _FrozenTree:
    direct_entries: dict[str, dict[str, _FrozenTree | TreeEntry]] = {}
    tree_entries: dict[str, TreeEntry] = {}
    for entry in entries:
        parent, separator, name = entry.path.rpartition("/")
        if not separator:
            parent = ""
            name = entry.path
        normalized = TreeEntry(
            mode=entry.mode,
            object_type=entry.object_type,
            oid=entry.oid,
            path=name,
        )
        direct_entries.setdefault(parent, {})[name] = normalized
        if entry.object_type == "tree":
            tree_entries[entry.path] = entry

    for path in sorted(tree_entries, key=lambda value: value.count("/"), reverse=True):
        tree_entry = tree_entries[path]
        node = _FrozenTree(oid=tree_entry.oid, entries=direct_entries.get(path, {}))
        parent, separator, name = path.rpartition("/")
        if not separator:
            parent = ""
            name = path
        direct_entries.setdefault(parent, {})[name] = node
    return _FrozenTree(oid=tree_oid, entries=direct_entries.get("", {}))


def load_projection_context(
    repo: Path,
    *,
    base_oid: str,
    final_oid: str,
    replay_atoms: Sequence[ReplayAtom],
) -> ProjectionContext:
    """Load one Base/Final snapshot for any number of slice projections."""

    base_tree_oid = rev_parse(repo, f"{base_oid}^{{tree}}")
    final_tree_oid = rev_parse(repo, f"{final_oid}^{{tree}}")
    final_date = commit_committer_date(repo, final_oid)
    base_tree_entries = ls_tree_recursive(repo, base_tree_oid)
    final_tree_entries = ls_tree_recursive(repo, final_tree_oid)

    atoms_by_path_lists: dict[str, list[ReplayAtom]] = {}
    for replay_atom in replay_atoms:
        atoms_by_path_lists.setdefault(replay_atom.atom.path, []).append(replay_atom)

    return ProjectionContext(
        repo=repo,
        base_oid=base_oid,
        final_oid=final_oid,
        base_tree_oid=base_tree_oid,
        final_tree_oid=final_tree_oid,
        final_committer_date=final_date,
        final_root=_fold_tree(final_tree_oid, final_tree_entries),
        base_entries={entry.path: entry for entry in base_tree_entries},
        atoms_by_path={path: tuple(items) for path, items in atoms_by_path_lists.items()},
        _blob_cache={},
    )


def _lookup_frozen_entry(root: _FrozenTree, path: str) -> TreeEntry | None:
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
    if isinstance(value, _FrozenTree):
        return None
    return value


def _lookup_base_entry(context: ProjectionContext, path: str) -> TreeEntry | None:
    value = context.base_entries.get(path)
    if value is None or value.object_type == "tree":
        return None
    return value


def _cow_get(node: _CowTree, name: str) -> _CowTree | _FrozenTree | TreeEntry | None:
    if name in node.changes:
        return node.changes[name]
    if node.frozen is None:
        return None
    return node.frozen.entries.get(name)


def _cow_items(node: _CowTree) -> list[tuple[str, _CowTree | _FrozenTree | TreeEntry]]:
    items: list[tuple[str, _CowTree | _FrozenTree | TreeEntry]] = []
    seen: set[str] = set()
    if node.frozen is not None:
        for name in node.frozen.entries:
            seen.add(name)
            value = _cow_get(node, name)
            if value is not None:
                items.append((name, value))
    for name, value in node.changes.items():
        if name not in seen and value is not None:
            items.append((name, value))
    return items


def _cow_is_empty(node: _CowTree) -> bool:
    return not _cow_items(node)


def _delete_parts(node: _CowTree, parts: tuple[str, ...], index: int) -> bool:
    name = parts[index]
    current = _cow_get(node, name)
    if current is None:
        return False
    if index == len(parts) - 1:
        node.changes[name] = None
        return True
    if isinstance(current, TreeEntry):
        return False

    child = current if isinstance(current, _CowTree) else _CowTree(frozen=current)
    changed = _delete_parts(child, parts, index + 1)
    if changed:
        node.changes[name] = None if _cow_is_empty(child) else child
    return changed


def _delete_path(root: _CowTree, path: str) -> None:
    _delete_parts(root, _path_parts(path), 0)


def _same_entry(left: TreeEntry, right: TreeEntry) -> bool:
    return (
        left.mode == right.mode and left.object_type == right.object_type and left.oid == right.oid
    )


def _set_parts(node: _CowTree, parts: tuple[str, ...], index: int, entry: TreeEntry) -> bool:
    name = parts[index]
    current = _cow_get(node, name)
    if index == len(parts) - 1:
        if isinstance(current, (_CowTree, _FrozenTree)):
            raise ProjectionError(
                f"cannot replace tree {'/'.join(parts)!r} with a file in one projection"
            )
        normalized = TreeEntry(
            mode=entry.mode,
            object_type=entry.object_type,
            oid=entry.oid,
            path=name,
        )
        if isinstance(current, TreeEntry) and _same_entry(current, normalized):
            return False
        node.changes[name] = normalized
        return True

    if isinstance(current, TreeEntry):
        raise ProjectionError(
            f"cannot create {'/'.join(parts)!r}: its parent component {name!r} is a file"
        )
    child = (
        current
        if isinstance(current, _CowTree)
        else _CowTree(frozen=current if isinstance(current, _FrozenTree) else None)
    )
    changed = _set_parts(child, parts, index + 1, entry)
    if changed:
        node.changes[name] = child
    return changed


def _set_path(root: _CowTree, path: str, entry: TreeEntry) -> None:
    _set_parts(root, _path_parts(path), 0, entry)


def _write_cow_tree(repo: Path, node: _CowTree) -> str:
    if not node.changes and node.frozen is not None:
        return node.frozen.oid

    entries: list[TreeEntry] = []
    unchanged = node.frozen is not None
    frozen_names = set(node.frozen.entries) if node.frozen is not None else set()
    current_names: set[str] = set()
    for name, value in _cow_items(node):
        current_names.add(name)
        if isinstance(value, (_CowTree, _FrozenTree)):
            oid = value.oid if isinstance(value, _FrozenTree) else _write_cow_tree(repo, value)
            entry = TreeEntry(mode="040000", object_type="tree", oid=oid, path=name)
        else:
            entry = TreeEntry(
                mode=value.mode,
                object_type=value.object_type,
                oid=value.oid,
                path=name,
            )
        entries.append(entry)
        if unchanged and node.frozen is not None:
            original = node.frozen.entries.get(name)
            if isinstance(original, _FrozenTree):
                unchanged = entry.object_type == "tree" and entry.oid == original.oid
            elif isinstance(original, TreeEntry):
                unchanged = _same_entry(entry, original)
            else:
                unchanged = False

    if unchanged and current_names == frozen_names and node.frozen is not None:
        return node.frozen.oid
    return mktree(repo, entries)


def _entry_content(
    context: ProjectionContext, entry: TreeEntry | None, *, path: str
) -> bytes | None:
    if entry is None:
        return None
    if entry.object_type != "blob":
        raise ProjectionError(f"text atom path {path!r} does not resolve to a blob")
    if entry.oid not in context._blob_cache:
        context._blob_cache[entry.oid] = cat_file(context.repo, entry.oid)
    return context._blob_cache[entry.oid]


def _synthetic_entry(
    context: ProjectionContext,
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
    base_content = _entry_content(context, base_entry, path=path)
    content = replay_file(base_content, non_slice_atoms)
    if content is None:
        return None

    mode_source = final_entry or base_entry
    if mode_source is None or mode_source.object_type != "blob":
        raise ProjectionError(f"could not determine blob mode for projected path {path!r}")
    oid = (
        base_entry.oid
        if base_entry is not None and base_content is not None and content == base_content
        else hash_object(context.repo, content)
    )
    return TreeEntry(
        mode=mode_source.mode,
        object_type="blob",
        oid=oid,
        path=_path_parts(path)[-1],
    )


def _build_projection(
    context: ProjectionContext, slice_id: str, owned_paths: set[str]
) -> ProjectionCommits:
    synthetic_root = _CowTree(frozen=context.final_root)
    desired_entries: dict[str, TreeEntry | None] = {}
    for path in owned_paths:
        desired_entries[path] = _synthetic_entry(
            context,
            path=path,
            slice_id=slice_id,
            path_atoms=context.atoms_by_path[path],
            base_entry=_lookup_base_entry(context, path),
            final_entry=_lookup_frozen_entry(context.final_root, path),
        )

    for path in sorted(desired_entries, key=lambda value: value.count("/"), reverse=True):
        _delete_path(synthetic_root, path)
    for path in sorted(desired_entries, key=lambda value: value.count("/")):
        entry = desired_entries[path]
        if entry is not None:
            _set_path(synthetic_root, path, entry)

    synthetic_tree_oid = _write_cow_tree(context.repo, synthetic_root)
    identity = GitIdentity(
        name=_PROJECTION_IDENTITY.name,
        email=_PROJECTION_IDENTITY.email,
        date=context.final_committer_date,
    )
    base_message = f"git-paoding projection base\nslice: {slice_id}\nfinal: {context.final_oid}\n"
    base_commit_oid = commit_tree(
        context.repo,
        synthetic_tree_oid,
        base_message,
        parents=(context.base_oid,),
        author=identity,
        committer=identity,
    )
    head_message = f"git-paoding projection head\nslice: {slice_id}\nfinal: {context.final_oid}\n"
    head_commit_oid = commit_tree(
        context.repo,
        context.final_tree_oid,
        head_message,
        parents=(base_commit_oid,),
        author=identity,
        committer=identity,
    )
    return ProjectionCommits(
        slice_id=slice_id,
        final_oid=context.final_oid,
        base_tree_oid=synthetic_tree_oid,
        head_tree_oid=context.final_tree_oid,
        base_commit_oid=base_commit_oid,
        head_commit_oid=head_commit_oid,
    )


def build_projections(
    context: ProjectionContext,
    slice_ids: Sequence[str],
) -> dict[str, ProjectionCommits]:
    """Build multiple projections from one immutable repository snapshot."""

    requested = set(slice_ids)
    if "" in requested:
        raise ProjectionError("slice id must not be empty")

    paths_by_owner: dict[str, set[str]] = {slice_id: set() for slice_id in requested}
    for path, path_atoms in context.atoms_by_path.items():
        for replay_atom in path_atoms:
            owner = replay_atom.atom.owner
            if owner in paths_by_owner:
                paths_by_owner[owner].add(path)

    return {
        slice_id: _build_projection(context, slice_id, paths_by_owner[slice_id])
        for slice_id in slice_ids
    }


def build_projection(
    repo: Path,
    *,
    base_oid: str,
    final_oid: str,
    slice_id: str,
    replay_atoms: Sequence[ReplayAtom],
) -> ProjectionCommits:
    """Build one projection through the shared-context implementation."""

    context = load_projection_context(
        repo,
        base_oid=base_oid,
        final_oid=final_oid,
        replay_atoms=replay_atoms,
    )
    return build_projections(context, (slice_id,))[slice_id]
