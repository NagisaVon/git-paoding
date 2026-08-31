"""Validation and round-trip tests for persistent domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from git_paoding.core.model import (
    Atom,
    AtomKind,
    AtomState,
    Session,
    Slice,
    SliceStatus,
)


def representative_session() -> Session:
    """Build a session containing every meaningful optional relationship."""

    return Session(
        canonical_branch="feature/storage-v2",
        base_ref="origin/main",
        base_oid="1" * 40,
        slices=[
            Slice(id="storage", title="Storage model", pr_number=101),
            Slice(id="api", title="Public API", status=SliceStatus.ARCHIVED),
        ],
        atoms=[
            Atom(
                atom_id="a1b2c3d4",
                path="src/store.py",
                kind=AtomKind.MODIFY,
                base_start=10,
                base_len=2,
                final_start=10,
                final_len=4,
                gap_seq=0,
                content_hash="2" * 64,
                owner="storage",
                state=AtomState.UPDATED,
                preview="+new storage API",
            ),
            Atom(
                atom_id="e5f6a7b8",
                path="tests/test_store.py",
                kind=AtomKind.ADD_FILE,
                base_start=0,
                base_len=0,
                final_start=1,
                final_len=12,
                gap_seq=0,
                content_hash="3" * 64,
                state=AtomState.UNASSIGNED,
                preview="+def test_store():",
            ),
        ],
        last_final_oid="4" * 40,
        focus_slice="storage",
        integration_pr=99,
    )


@pytest.mark.unit
def test_session_json_round_trip_preserves_identity() -> None:
    session = representative_session()

    restored = Session.model_validate_json(session.model_dump_json())

    assert restored == session
    assert restored.model_dump(mode="json") == session.model_dump(mode="json")


@pytest.mark.unit
def test_slice_id_is_slug_validated() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        Slice(id="Storage API", title="Invalid")


@pytest.mark.unit
def test_slice_pr_prefix_accepts_ticket_identifiers_and_rejects_unsafe_text() -> None:
    session = Session(
        canonical_branch="feature/x",
        base_oid="1" * 40,
        slice_pr_prefix="Team/ABC_123.4",
    )

    assert session.slice_pr_prefix == "Team/ABC_123.4"
    for invalid in ("", "two words", "line\nbreak", "x" * 41, "[ticket]"):
        with pytest.raises(ValidationError):
            Session(
                canonical_branch="feature/x",
                base_oid="1" * 40,
                slice_pr_prefix=invalid,
            )


@pytest.mark.unit
def test_atom_owner_must_match_attribution_state() -> None:
    with pytest.raises(ValidationError, match="requires an owner"):
        Atom(
            atom_id="deadbeef",
            path="file.py",
            kind=AtomKind.MODIFY,
            base_start=1,
            base_len=1,
            final_start=1,
            final_len=1,
            content_hash="hash",
            state=AtomState.ASSIGNED,
        )


@pytest.mark.unit
def test_session_rejects_duplicate_and_dangling_identities() -> None:
    with pytest.raises(ValidationError, match="slice ids must be unique"):
        Session(
            canonical_branch="feature/x",
            base_oid="1" * 40,
            slices=[Slice(id="same", title="One"), Slice(id="same", title="Two")],
        )

    with pytest.raises(ValidationError, match="unknown slices: missing"):
        Session(
            canonical_branch="feature/x",
            base_oid="1" * 40,
            atoms=[
                Atom(
                    atom_id="deadbeef",
                    path="file.py",
                    kind=AtomKind.MODIFY,
                    base_start=1,
                    base_len=1,
                    final_start=1,
                    final_len=1,
                    content_hash="hash",
                    owner="missing",
                    state=AtomState.ASSIGNED,
                )
            ],
        )
