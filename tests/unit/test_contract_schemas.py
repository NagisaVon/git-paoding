"""Golden snapshots for the three public JSON contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from git_paoding.core.model import (
    AssignBatchRequest,
    Atom,
    AtomKind,
    AtomState,
    DiffStat,
    PublishOutcome,
    PublishResult,
    PublishSliceResult,
    SessionSummary,
    SliceStatus,
    SliceSummary,
    StatusResult,
)

SCHEMA_DIR = Path(__file__).parents[2] / "schemas"
PAYLOAD_DIR = Path(__file__).parents[1] / "golden" / "contracts"


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


def _status_payload() -> StatusResult:
    return StatusResult(
        session=SessionSummary(
            canonical_branch="feature/review-slices",
            base_ref="origin/main",
            base_oid="1" * 40,
            last_final_oid="2" * 40,
            integration_pr=41,
        ),
        slices=[
            SliceSummary(
                id="review",
                title="Review behavior",
                status=SliceStatus.ACTIVE,
                pr_number=42,
                diffstat=DiffStat(files_changed=1, additions=1, deletions=1),
            )
        ],
        atoms=[
            Atom(
                atom_id="a1b2c3d4",
                path="scenario.txt",
                kind=AtomKind.MODIFY,
                base_start=2,
                base_len=1,
                final_start=2,
                final_len=1,
                content_hash="3" * 64,
                owner="review",
                state=AtomState.ASSIGNED,
                preview='-alpha = "base"\n+alpha = "slice"',
            )
        ],
    )


def _assign_batch_payload() -> AssignBatchRequest:
    return AssignBatchRequest(
        assignments={"review": ["a1b2c3d4", "src/review.py"]},
        force=False,
    )


def _publish_payload() -> PublishResult:
    return PublishResult(
        slices=[
            PublishSliceResult(
                slice_id="review",
                title="Review behavior",
                outcome=PublishOutcome.NO_OP,
                pr_number=42,
                url="https://github.com/example/git-paoding/pull/42",
            ),
            PublishSliceResult(
                slice_id="empty-check",
                title="Empty slice contract",
                outcome=PublishOutcome.EMPTY,
            ),
        ],
        integration_pr=41,
        integration_pr_url="https://github.com/example/git-paoding/pull/41",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("status.v0.json", _status_payload()),
        ("assign-batch.v0.json", _assign_batch_payload()),
        ("publish.v0.json", _publish_payload()),
    ],
)
def test_contract_payload_matches_v0_golden(filename: str, payload: BaseModel) -> None:
    golden = (PAYLOAD_DIR / filename).read_text(encoding="utf-8")

    assert golden == payload.model_dump_json(indent=2) + "\n"
    assert type(payload).model_validate_json(golden) == payload
