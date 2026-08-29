"""Parse zero-context Git diffs into raw, Base-anchored hunk records."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from git_paoding.gitio.runner import run_git

_DIFF_HEADER = re.compile(r'^diff --git (?P<base>"(?:\\.|[^"])*"|\S+) (?P<final>.+)$')
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<base_start>\d+)(?:,(?P<base_len>\d+))? "
    r"\+(?P<final_start>\d+)(?:,(?P<final_len>\d+))? @@"
)


@dataclass(frozen=True, slots=True)
class RawDiffHunk:
    """One parsed hunk, or one sentinel record for a non-text file change."""

    path: str
    base_start: int
    base_len: int
    final_start: int
    final_len: int
    removed_lines: tuple[str, ...]
    added_lines: tuple[str, ...]
    is_add_file: bool = False
    is_delete_file: bool = False
    is_binary: bool = False
    is_mode_change: bool = False
    is_symlink: bool = False
    no_newline_at_eof: bool = False
    base_oid: str | None = None
    final_oid: str | None = None
    base_mode: str | None = None
    final_mode: str | None = None


@dataclass(frozen=True, slots=True)
class _RawFileChange:
    """Object-database identity for one changed path."""

    path: str
    base_oid: str | None
    final_oid: str | None
    base_mode: str | None
    final_mode: str | None


@dataclass(slots=True)
class _MutableHunk:
    base_start: int
    base_len: int
    final_start: int
    final_len: int
    removed_lines: list[str] = field(default_factory=list)
    added_lines: list[str] = field(default_factory=list)
    no_newline_at_eof: bool = False
    last_line_kind: str | None = None


@dataclass(slots=True)
class _FileDiff:
    path: str
    hunks: list[_MutableHunk] = field(default_factory=list)
    is_add_file: bool = False
    is_delete_file: bool = False
    is_binary: bool = False
    is_mode_change: bool = False
    is_symlink: bool = False


def _decode_header_path(value: str) -> str:
    if value.startswith('"'):
        decoded = ast.literal_eval(value)
        if not isinstance(decoded, str):
            raise ValueError(f"Invalid quoted Git path: {value}")
        return decoded
    return value


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _finalize_file(file_diff: _FileDiff | None, records: list[RawDiffHunk]) -> None:
    if file_diff is None:
        return
    hunks = file_diff.hunks or [_MutableHunk(0, 0, 0, 0)]
    for hunk in hunks:
        records.append(
            RawDiffHunk(
                path=file_diff.path,
                base_start=hunk.base_start,
                base_len=hunk.base_len,
                final_start=hunk.final_start,
                final_len=hunk.final_len,
                removed_lines=tuple(hunk.removed_lines),
                added_lines=tuple(hunk.added_lines),
                is_add_file=file_diff.is_add_file,
                is_delete_file=file_diff.is_delete_file,
                is_binary=file_diff.is_binary,
                is_mode_change=file_diff.is_mode_change,
                is_symlink=file_diff.is_symlink,
                no_newline_at_eof=hunk.no_newline_at_eof,
            )
        )


def parse_diff(diff: bytes | str) -> tuple[RawDiffHunk, ...]:
    """Parse output from ``git diff -U0 --no-renames``."""

    text = diff.decode("utf-8", errors="surrogateescape") if isinstance(diff, bytes) else diff
    records: list[RawDiffHunk] = []
    current_file: _FileDiff | None = None
    current_hunk: _MutableHunk | None = None

    for line_with_end in text.splitlines(keepends=True):
        line = line_with_end.removesuffix("\n")
        header_match = _DIFF_HEADER.match(line)
        if header_match is not None:
            _finalize_file(current_file, records)
            base_path = _decode_header_path(header_match.group("base"))
            final_path = _decode_header_path(header_match.group("final"))
            path = _strip_prefix(final_path if final_path != "/dev/null" else base_path)
            current_file = _FileDiff(path=path)
            current_hunk = None
            continue
        if current_file is None:
            continue

        if line.startswith("new file mode "):
            current_file.is_add_file = True
            current_file.is_symlink = line.endswith(" 120000")
            continue
        if line.startswith("deleted file mode "):
            current_file.is_delete_file = True
            current_file.is_symlink = line.endswith(" 120000")
            continue
        if line.startswith("old mode ") or line.startswith("new mode "):
            current_file.is_mode_change = True
            if line.endswith(" 120000"):
                current_file.is_symlink = True
            continue
        if line.startswith("index ") and line.endswith(" 120000"):
            current_file.is_symlink = True
            continue
        if line.startswith("Binary files ") or line == "GIT binary patch":
            current_file.is_binary = True
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match is not None:
            current_hunk = _MutableHunk(
                base_start=int(hunk_match.group("base_start")),
                base_len=int(hunk_match.group("base_len") or "1"),
                final_start=int(hunk_match.group("final_start")),
                final_len=int(hunk_match.group("final_len") or "1"),
            )
            current_file.hunks.append(current_hunk)
            continue
        if current_hunk is None:
            continue

        if line.startswith("-"):
            current_hunk.removed_lines.append(line_with_end[1:])
            current_hunk.last_line_kind = "removed"
        elif line.startswith("+"):
            current_hunk.added_lines.append(line_with_end[1:])
            current_hunk.last_line_kind = "added"
        elif line == r"\ No newline at end of file":
            current_hunk.no_newline_at_eof = True
            target = (
                current_hunk.removed_lines
                if current_hunk.last_line_kind == "removed"
                else current_hunk.added_lines
            )
            if target and target[-1].endswith("\n"):
                target[-1] = target[-1][:-1]

    _finalize_file(current_file, records)
    return tuple(records)


def _optional_raw_value(value: bytes) -> str | None:
    decoded = value.decode("ascii")
    return None if not decoded.strip("0") else decoded


def _parse_raw_changes(raw_diff: bytes) -> dict[str, _RawFileChange]:
    """Parse ``git diff --raw -z`` metadata without losing unusual path bytes."""

    fields = raw_diff.split(b"\0")
    changes: dict[str, _RawFileChange] = {}
    index = 0
    while index < len(fields) and fields[index]:
        metadata = fields[index]
        if index + 1 >= len(fields):
            raise ValueError("Raw Git diff ended before its path field")
        raw_path = fields[index + 1]
        parts = metadata.removeprefix(b":").split(b" ")
        if len(parts) != 5:
            raise ValueError(f"Unexpected raw Git diff metadata: {metadata!r}")
        base_mode, final_mode, base_oid, final_oid, status = parts
        if status.startswith((b"R", b"C")):
            raise ValueError("Raw Git diff unexpectedly reported a rename or copy")
        path = raw_path.decode("utf-8", errors="surrogateescape")
        changes[path] = _RawFileChange(
            path=path,
            base_oid=_optional_raw_value(base_oid),
            final_oid=_optional_raw_value(final_oid),
            base_mode=_optional_raw_value(base_mode),
            final_mode=_optional_raw_value(final_mode),
        )
        index += 2
    return changes


def diff_trees(repo: Path, base: str, final: str) -> tuple[RawDiffHunk, ...]:
    """Read and parse a deterministic, zero-context tree diff."""

    patch_output = run_git(
        (
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            "--unified=0",
            "--no-renames",
            base,
            final,
            "--",
        ),
        cwd=repo,
    ).stdout
    raw_output = run_git(
        (
            "diff",
            "--raw",
            "-z",
            "--abbrev=40",
            "--no-renames",
            base,
            final,
            "--",
        ),
        cwd=repo,
    ).stdout
    changes = _parse_raw_changes(raw_output)
    enriched: list[RawDiffHunk] = []
    for hunk in parse_diff(patch_output):
        try:
            change = changes[hunk.path]
        except KeyError as error:
            raise ValueError(f"Patch path missing from raw Git diff: {hunk.path!r}") from error
        enriched.append(
            replace(
                hunk,
                base_oid=change.base_oid,
                final_oid=change.final_oid,
                base_mode=change.base_mode,
                final_mode=change.final_mode,
            )
        )
    return tuple(enriched)
