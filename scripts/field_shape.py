"""Deterministic field-shaped repository generator for release validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

DIRECTORY_COUNT = 315
CHANGED_FILE_COUNT = 33
ATOM_COUNT = 36
SLICE_COUNT = 7


@dataclass(frozen=True, slots=True)
class FieldShape:
    """Generated Base/Final states and their architectural tree-write bound."""

    base: dict[str, str]
    final: dict[str, str]
    changed_paths: tuple[str, ...]
    slice_paths: dict[str, tuple[str, ...]]
    dirty_ancestor_bound: int


def _content(index: int, *, final: bool) -> str:
    lines = [f"file-{index:03d} line-{line:02d} base\n" for line in range(24)]
    changed_lines = (2, 18) if index < 3 else (10,)
    if final:
        for line in changed_lines:
            lines[line] = f"file-{index:03d} line-{line:02d} final\n"
    return "".join(lines)


def _ancestor_directories(path: str) -> set[str]:
    parent = PurePosixPath(path).parent
    ancestors = {""}
    while parent != PurePosixPath("."):
        ancestors.add(parent.as_posix())
        parent = parent.parent
    return ancestors


def build_field_shape() -> FieldShape:
    """Build 315 directories, 33 changed files, 36 hunks, and seven slices."""

    base = {
        f"tree-{index:03d}/placeholder.txt": f"unchanged directory {index:03d}\n"
        for index in range(DIRECTORY_COUNT)
    }
    final = dict(base)
    changed_paths = tuple(
        f"tree-{index:03d}/changed-{index:03d}.txt" for index in range(CHANGED_FILE_COUNT)
    )
    for index, path in enumerate(changed_paths):
        base[path] = _content(index, final=False)
        final[path] = _content(index, final=True)

    slice_paths = {
        f"slice-{index + 1}": tuple(changed_paths[index::SLICE_COUNT])
        for index in range(SLICE_COUNT)
    }
    dirty_ancestor_bound = sum(
        len(set().union(*(_ancestor_directories(path) for path in paths)))
        for paths in slice_paths.values()
    )
    return FieldShape(
        base=base,
        final=final,
        changed_paths=changed_paths,
        slice_paths=slice_paths,
        dirty_ancestor_bound=dirty_ancestor_bound,
    )
