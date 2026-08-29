"""Golden-file tests for zero-context diff parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from git_paoding.gitio.diffparse import RawDiffHunk, parse_diff

GOLDEN_DIR = Path(__file__).parents[1] / "golden" / "diffs"


def _load_expected(path: Path) -> tuple[RawDiffHunk, ...]:
    payload = cast(list[dict[str, Any]], json.loads(path.read_text()))
    records: list[RawDiffHunk] = []
    for item in payload:
        item["removed_lines"] = tuple(item["removed_lines"])
        item["added_lines"] = tuple(item["added_lines"])
        records.append(RawDiffHunk(**item))
    return tuple(records)


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["modify", "add", "delete", "binary", "mode", "symlink", "no-newline"],
)
def test_parse_diff_golden(case: str) -> None:
    diff = (GOLDEN_DIR / f"{case}.diff").read_bytes()
    expected = _load_expected(GOLDEN_DIR / f"{case}.json")

    assert parse_diff(diff) == expected
