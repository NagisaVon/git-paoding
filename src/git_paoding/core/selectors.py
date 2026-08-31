"""Resolve author-facing selectors into atomic attribution updates."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import PurePosixPath

from git_paoding.core.model import (
    AssignmentRecord,
    AssignResult,
    Atom,
    AtomState,
    PaodingError,
)

_ATOM_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[1-9][0-9]*)?$")
_RANGE_RE = re.compile(r"^(?P<path>.+):(?P<start>[1-9][0-9]*)-(?P<end>[1-9][0-9]*)$")
_GLOB_CHARACTERS = frozenset("*?[")
_HINT_LIMIT = 3


class SelectorError(PaodingError):
    """Base class for selector validation and resolution failures."""


class SelectorNotFoundError(SelectorError):
    """An input could not be resolved to any atom in the current diff."""


class SelectorConflictError(SelectorError):
    """Raised when one batch assigns the same atom to different slices."""


class UnknownBatchSliceError(SelectorError):
    """Raised when a batch names a slice outside the active slice set."""


@dataclass(frozen=True, slots=True)
class _ResolvedSelector:
    indexes: tuple[int, ...]
    explicit_atom_id: bool


def _display_final_range(atom: Atom) -> str:
    if atom.final_len == 0:
        return f"gap@{atom.final_start}"
    return f"{atom.final_start}-{atom.final_start + atom.final_len - 1}"


def _repository_path(value: str) -> str:
    return value[2:] if value.startswith("./") else value


def _nearby_path_hints(atoms: Sequence[Atom], selector: str) -> str:
    paths = list(dict.fromkeys(atom.path for atom in atoms))
    normalized = selector.rstrip("/")
    parent = normalized.rpartition("/")[0]
    candidates = [path for path in paths if parent and path.startswith(f"{parent}/")]
    if not candidates:
        candidates = get_close_matches(normalized, paths, n=_HINT_LIMIT, cutoff=0.25)
    if not candidates:
        candidates = paths[:_HINT_LIMIT]
    if not candidates:
        return " The current diff contains no atoms."
    return f" Nearby paths: {', '.join(repr(path) for path in candidates[:_HINT_LIMIT])}."


def _nearby_range_hints(atoms: Sequence[Atom], *, path: str, start: int, end: int) -> str:
    same_file = [atom for atom in atoms if atom.path == path]
    if not same_file:
        return _nearby_path_hints(atoms, path)

    def distance(atom: Atom) -> int:
        if atom.final_len == 0:
            return min(abs(atom.final_start - start), abs(atom.final_start - end))
        atom_start = atom.final_start
        atom_end = atom.final_start + atom.final_len - 1
        return min(abs(atom_start - end), abs(start - atom_end))

    nearby = sorted(same_file, key=lambda atom: (distance(atom), atom.final_start, atom.atom_id))
    rendered = ", ".join(
        f"{atom.atom_id} final:{_display_final_range(atom)}" for atom in nearby[:_HINT_LIMIT]
    )
    return f" Nearby atoms in {path!r}: {rendered}."


def _resolve_selector(atoms: Sequence[Atom], selector: str) -> _ResolvedSelector:
    if not selector:
        raise SelectorError("Selectors must not be empty")

    by_id = {atom.atom_id: index for index, atom in enumerate(atoms)}
    if selector in by_id:
        return _ResolvedSelector((by_id[selector],), explicit_atom_id=True)

    if _ATOM_ID_RE.fullmatch(selector):
        current_ids = ", ".join(atom.atom_id for atom in atoms[:_HINT_LIMIT]) or "(none)"
        raise SelectorNotFoundError(
            f"Atom id {selector!r} is stale or unknown; run `git-paoding status` again. "
            f"Current atom ids include: {current_ids}."
        )

    normalized_selector = _repository_path(selector)
    exact_path = tuple(
        index for index, atom in enumerate(atoms) if atom.path == normalized_selector
    )
    if exact_path:
        return _ResolvedSelector(exact_path, explicit_atom_id=False)

    range_match = _RANGE_RE.fullmatch(selector)
    if range_match is not None:
        path = _repository_path(range_match.group("path"))
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        if end < start:
            raise SelectorError(
                f"Invalid Final line range {selector!r}: end must be greater than or equal to start"
            )
        indexes = tuple(
            index
            for index, atom in enumerate(atoms)
            if atom.path == path
            and atom.final_len > 0
            and atom.final_start <= end
            and atom.final_start + atom.final_len - 1 >= start
        )
        if not indexes:
            raise SelectorNotFoundError(
                f"Final-coordinate range {selector!r} matched no atom."
                + _nearby_range_hints(atoms, path=path, start=start, end=end)
            )
        return _ResolvedSelector(indexes, explicit_atom_id=False)

    normalized_directory = _repository_path(selector.removesuffix("/"))
    directory_matches = tuple(
        index
        for index, atom in enumerate(atoms)
        if normalized_directory in {"", "."} or atom.path.startswith(f"{normalized_directory}/")
    )
    if directory_matches:
        return _ResolvedSelector(directory_matches, explicit_atom_id=False)

    if any(character in selector for character in _GLOB_CHARACTERS):
        normalized_glob = _repository_path(selector)
        glob_matches = tuple(
            index
            for index, atom in enumerate(atoms)
            if PurePosixPath(atom.path).match(normalized_glob)
        )
        if glob_matches:
            return _ResolvedSelector(glob_matches, explicit_atom_id=False)

    raise SelectorNotFoundError(
        f"Selector {selector!r} matched no atom in the current diff by id, path, directory, "
        "glob, or line range." + _nearby_path_hints(atoms, selector)
    )


def _resolve_selectors(
    atoms: Sequence[Atom], selectors: Sequence[str]
) -> tuple[tuple[int, bool], ...]:
    if not selectors:
        raise SelectorError(
            "At least one atom id, path, directory, glob, or line-range selector is required"
        )

    selected: dict[int, bool] = {}
    for selector in selectors:
        resolved = _resolve_selector(atoms, selector)
        for index in resolved.indexes:
            selected[index] = selected.get(index, False) or resolved.explicit_atom_id
    return tuple(selected.items())


def _assignment_record(atom: Atom, *, owner: str, previous_owner: str | None) -> AssignmentRecord:
    return AssignmentRecord(
        atom_id=atom.atom_id,
        path=atom.path,
        previous_owner=previous_owner,
        owner=owner,
        preview=atom.preview,
    )


def _apply_plan(
    atoms: Sequence[Atom],
    plan: Sequence[tuple[int, str, bool]],
    *,
    force: bool,
) -> tuple[tuple[Atom, ...], AssignResult]:
    updated_atoms = list(atoms)
    assigned: list[AssignmentRecord] = []
    skipped: list[AssignmentRecord] = []

    for index, slice_id, explicit_atom_id in plan:
        atom = updated_atoms[index]
        previous_owner = atom.owner
        if previous_owner == slice_id:
            skipped.append(_assignment_record(atom, owner=slice_id, previous_owner=previous_owner))
            continue
        if previous_owner is not None and not (force or explicit_atom_id):
            skipped.append(
                _assignment_record(atom, owner=previous_owner, previous_owner=previous_owner)
            )
            continue

        updated_atoms[index] = atom.model_copy(
            update={"owner": slice_id, "state": AtomState.ASSIGNED}
        )
        assigned.append(_assignment_record(atom, owner=slice_id, previous_owner=previous_owner))

    return tuple(updated_atoms), AssignResult(assigned=assigned, skipped=skipped)


def assign_selectors(
    atoms: Sequence[Atom],
    *,
    slice_id: str,
    selectors: Sequence[str],
    force: bool = False,
) -> tuple[tuple[Atom, ...], AssignResult]:
    """Resolve selectors and assign their atoms to one slice atomically.

    Exact atom ids may take ownership without ``force``. Broader selectors
    preserve already-owned atoms unless ``force`` is true. Every selector is
    resolved before any updated atom tuple is produced.
    """

    resolved = _resolve_selectors(atoms, selectors)
    plan = tuple((index, slice_id, explicit_atom_id) for index, explicit_atom_id in resolved)
    return _apply_plan(atoms, plan, force=force)


def assign_batch_selectors(
    atoms: Sequence[Atom],
    *,
    assignments: Mapping[str, Sequence[str]],
    active_slice_ids: Collection[str],
    force: bool = False,
) -> tuple[tuple[Atom, ...], AssignResult]:
    """Validate and resolve an entire multi-slice assignment plan before applying it.

    A batch that names an unknown slice, contains an invalid selector, or assigns
    one atom to different slices fails without returning any mutated atoms.
    """

    if not assignments:
        raise SelectorError("Batch assignments must contain at least one slice")

    active = set(active_slice_ids)
    unknown = [slice_id for slice_id in assignments if slice_id not in active]
    if unknown:
        rendered = ", ".join(repr(slice_id) for slice_id in unknown)
        raise UnknownBatchSliceError(f"Batch names unknown or inactive slices: {rendered}")

    plan_by_index: dict[int, tuple[str, bool]] = {}
    ordered_indexes: list[int] = []
    for slice_id, selectors in assignments.items():
        resolved = _resolve_selectors(atoms, selectors)
        for index, explicit_atom_id in resolved:
            prior = plan_by_index.get(index)
            if prior is not None and prior[0] != slice_id:
                atom = atoms[index]
                raise SelectorConflictError(
                    f"Batch assigns atom {atom.atom_id!r} ({atom.path}) to both "
                    f"{prior[0]!r} and {slice_id!r}"
                )
            if prior is None:
                ordered_indexes.append(index)
                plan_by_index[index] = (slice_id, explicit_atom_id)
            else:
                plan_by_index[index] = (slice_id, prior[1] or explicit_atom_id)

    plan = tuple(
        (index, plan_by_index[index][0], plan_by_index[index][1]) for index in ordered_indexes
    )
    return _apply_plan(atoms, plan, force=force)
