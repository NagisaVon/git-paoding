"""Golden snapshots for the three public JSON contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from git_paoding.core.model import AssignBatchRequest, PublishResult, StatusResult

SCHEMA_DIR = Path(__file__).parents[2] / "schemas"


def _render_schema(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("status.schema.json", StatusResult),
        ("assign-batch.schema.json", AssignBatchRequest),
        ("publish.schema.json", PublishResult),
    ],
)
def test_contract_schema_matches_exported_golden(filename: str, model: type[BaseModel]) -> None:
    assert (SCHEMA_DIR / filename).read_text(encoding="utf-8") == _render_schema(model)
