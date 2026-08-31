"""Tests for scalable status aggregation and quiet assignment output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from git_paoding import api
from git_paoding.cli import main as cli_main
from git_paoding.core.model import (
    AssignmentRecord,
    AssignResult,
    Atom,
    AtomKind,
    AtomState,
    Session,
    SessionSummary,
    StatusResult,
    StatusView,
)


def _atom(
    atom_id: str,
    path: str,
    state: AtomState,
    *,
    owner: str | None = None,
    additions: int = 1,
    deletions: int = 1,
) -> Atom:
    return Atom(
        atom_id=atom_id,
        path=path,
        kind=AtomKind.MODIFY,
        base_start=1,
        base_len=deletions,
        final_start=1,
        final_len=additions,
        content_hash=f"hash-{atom_id}",
        owner=owner,
        state=state,
        preview=f"preview for {atom_id}",
    )


def _mixed_status() -> StatusResult:
    atoms = [
        _atom("a1", "src/a.py", AtomState.ASSIGNED, owner="zeta", additions=2),
        _atom(
            "a2",
            "src/a.py",
            AtomState.UPDATED,
            owner="alpha",
            additions=3,
            deletions=0,
        ),
        _atom("a3", "src/a.py", AtomState.UNASSIGNED, additions=0, deletions=2),
        _atom("a4", "src/a.py", AtomState.AMBIGUOUS, additions=4, deletions=5),
        _atom("b1", "src/b.py", AtomState.ASSIGNED, owner="alpha"),
    ]
    return StatusResult(
        session=SessionSummary(canonical_branch="main", base_oid="base"),
        atoms=atoms,
        unassigned_count=1,
        ambiguous_count=1,
    )


def _stub_status_source(monkeypatch: pytest.MonkeyPatch, status: StatusResult) -> None:
    session = Session(canonical_branch="main", base_oid="base")
    monkeypatch.setattr(api, "_canonical_branch", lambda repo, requested: "main")
    monkeypatch.setattr(
        api, "JsonSessionStore", lambda repo: SimpleNamespace(load=lambda _: session)
    )
    monkeypatch.setattr(
        api,
        "reconcile_and_status",
        lambda repo, loaded, full=False: (loaded, (), status),
    )


@pytest.mark.unit
def test_path_view_aggregates_mixed_states_repeated_paths_and_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _mixed_status()
    _stub_status_source(monkeypatch, status)

    result = api.get_status_view(
        Path("."),
        view=StatusView.PATHS,
        action_needed_only=True,
    )

    assert result.total_atom_count == 5
    assert result.unassigned_count == 1
    assert result.ambiguous_count == 1
    assert result.returned_atom_count == 4
    assert result.atoms is None
    assert result.paths is not None
    assert [summary.model_dump() for summary in result.paths] == [
        {
            "path": "src/a.py",
            "atom_count": 4,
            "assigned_count": 1,
            "unassigned_count": 1,
            "ambiguous_count": 1,
            "updated_count": 1,
            "owners": ["alpha", "zeta"],
            "additions": 9,
            "deletions": 8,
        }
    ]
    assert "preview" not in json.dumps(result.paths[0].model_dump())


@pytest.mark.unit
def test_atom_filters_keep_global_counts_when_no_records_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _mixed_status()
    _stub_status_source(monkeypatch, status)

    result = api.get_status_view(
        Path("."),
        view=StatusView.ATOMS,
        paths=["src/b.py"],
        action_needed_only=True,
    )

    assert result.total_atom_count == 5
    assert result.unassigned_count == result.ambiguous_count == 1
    assert result.returned_atom_count == 0
    assert result.path_filters == ["src/b.py"]
    assert result.paths is None
    assert result.atoms == []


@pytest.mark.unit
def test_quiet_json_assignment_preserves_contract_and_blanks_every_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = AssignResult(
        assigned=[
            AssignmentRecord(
                atom_id="a1",
                path="src/a.py",
                previous_owner="old",
                owner="review",
                preview="-old\n+new",
            )
        ],
        skipped=[
            AssignmentRecord(
                atom_id="b1",
                path="src/b.py",
                previous_owner="other",
                owner="other",
                preview="+kept",
            )
        ],
    )

    def fake_assign(
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult:
        return result

    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(assign=fake_assign))

    invocation = CliRunner().invoke(
        cli_main.main,
        ["assign", "review", "a1", "--quiet", "--json"],
    )

    assert invocation.exit_code == 0
    raw = json.loads(invocation.output)
    parsed = AssignResult.model_validate(raw)
    assert parsed.contract_version == 0
    assert all(record["preview"] == "" for record in [*raw["assigned"], *raw["skipped"]])
    assert parsed.assigned[0].previous_owner == "old"
    assert parsed.assigned[0].owner == "review"


@pytest.mark.unit
def test_quiet_human_assignment_emits_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assignment = AssignResult(
        assigned=[
            AssignmentRecord(
                atom_id="a1",
                path="src/a.py",
                owner="review",
                preview="secret preview",
            )
        ],
        skipped=[AssignmentRecord(atom_id="b1", path="src/b.py", preview="other preview")],
    )
    monkeypatch.setattr(
        cli_main,
        "_facade",
        SimpleNamespace(assign=lambda *args, **kwargs: assignment),
    )

    invocation = CliRunner().invoke(
        cli_main.main,
        ["assign", "review", "a1", "--quiet"],
    )

    assert invocation.exit_code == 0
    assert invocation.output == "Assigned: 1  Skipped: 1\n"
