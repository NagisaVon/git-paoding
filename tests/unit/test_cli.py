"""Tests for the thin Click shell and its exit/JSON contracts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import git_paoding.api as facade_api
from git_paoding import __version__
from git_paoding.cli import main as cli_main
from git_paoding.core.model import (
    AssignmentRecord,
    AssignResult,
    Atom,
    AtomKind,
    AtomState,
    PaodingError,
    PRState,
    PublishResult,
    PullRequestTarget,
    ReplaceResult,
    SessionSummary,
    SliceStatus,
    SliceSummary,
    StatusResult,
)
from git_paoding.core.progress import ProgressEvent, PublishPhase
from git_paoding.core.selectors import assign_batch_selectors


def _status() -> StatusResult:
    return StatusResult(
        session=SessionSummary(
            canonical_branch="main",
            base_ref="base",
            base_oid="base-oid",
            last_final_oid="final-oid",
        ),
        atoms=[
            Atom(
                atom_id="a1",
                path="app.py",
                kind=AtomKind.MODIFY,
                base_start=1,
                base_len=1,
                final_start=1,
                final_len=1,
                content_hash="hash",
                state=AtomState.UNASSIGNED,
                preview="-old\n+new",
            )
        ],
        unassigned_count=1,
    )


@pytest.mark.unit
def test_version_option() -> None:
    result = CliRunner().invoke(cli_main.main, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.unit
def test_status_json_emits_versioned_facade_result(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _status()

    def fake_get_status(repo: Path) -> StatusResult:
        assert repo == Path.cwd()
        return expected

    monkeypatch.setattr(facade_api, "get_status", fake_get_status)

    result = CliRunner().invoke(cli_main.main, ["status", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.output) == expected.model_dump(mode="json")
    assert json.loads(result.output)["contract_version"] == 0


@pytest.mark.unit
def test_clean_status_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _status().model_copy(update={"atoms": [], "unassigned_count": 0})

    def fake_get_status(repo: Path) -> StatusResult:
        return expected

    monkeypatch.setattr(facade_api, "get_status", fake_get_status)

    result = CliRunner().invoke(cli_main.main, ["status"])

    assert result.exit_code == 0
    assert "Action needed: 0 unassigned, 0 ambiguous" in result.output


@pytest.mark.unit
def test_init_slice_add_and_assign_dispatch_only_through_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _status()
    calls: list[tuple[object, ...]] = []

    def fake_backend(repo: Path) -> object:
        raise AssertionError(f"init --base must not construct a backend for {repo}")

    def fake_init(
        repo: Path,
        base: str,
        *,
        slice_pr_prefix: str,
    ) -> StatusResult:
        calls.append(("init", repo, base, slice_pr_prefix))
        return status

    def fake_add(repo: Path, slice_id: str, title: str) -> StatusResult:
        calls.append(("add", repo, slice_id, title))
        return status.model_copy(
            update={
                "slices": [
                    SliceSummary(
                        id=slice_id,
                        title=title,
                        status=SliceStatus.ACTIVE,
                    )
                ]
            }
        )

    def fake_assign(
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult:
        calls.append(("assign", repo, slice_id, tuple(selectors), force))
        return AssignResult(
            assigned=[
                AssignmentRecord(
                    atom_id="a1",
                    path="app.py",
                    owner=slice_id,
                    preview="-old\n+new",
                )
            ]
        )

    monkeypatch.setattr(cli_main, "_backend", fake_backend)
    monkeypatch.setattr(facade_api, "init_session", fake_init)
    monkeypatch.setattr(facade_api, "add_slice", fake_add)
    monkeypatch.setattr(facade_api, "assign", fake_assign)
    runner = CliRunner()

    init_result = runner.invoke(
        cli_main.main,
        ["init", "--base", "origin/main", "--slice-prefix", "ABC-123"],
    )
    add_result = runner.invoke(
        cli_main.main,
        ["slice", "add", "storage", "--title", "Storage"],
    )
    assign_result = runner.invoke(cli_main.main, ["assign", "storage", "a1", "app.py"])

    assert init_result.exit_code == add_result.exit_code == assign_result.exit_code == 0
    assert calls == [
        ("init", Path.cwd(), "origin/main", "ABC-123"),
        ("add", Path.cwd(), "storage", "Storage"),
        ("assign", Path.cwd(), "storage", ("a1", "app.py"), False),
    ]
    assert "assigned a1 app.py -> storage" in assign_result.output
    assert "-old" in assign_result.output
    assert "Added slice: storage" in add_result.output
    assert "Title: Storage" in add_result.output
    assert "Slices: 1 active" in add_result.output
    assert "Action needed: 1 unassigned, 0 ambiguous" in add_result.output
    assert "Atoms:" not in add_result.output
    assert "a1 app.py" not in add_result.output


@pytest.mark.unit
@pytest.mark.parametrize("arguments", [["init"], ["init", "--base", "main", "--pr", "73"]])
def test_init_requires_exactly_one_source_before_backend_access(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(
        cli_main,
        "_backend",
        lambda repo: (_ for _ in ()).throw(AssertionError("backend must not be constructed")),
    )

    result = CliRunner().invoke(cli_main.main, arguments)

    assert result.exit_code == 2
    assert "Exactly one of --base or --pr is required" in result.output


@pytest.mark.unit
def test_init_pr_checks_backend_and_resolves_once(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status().model_copy(
        update={
            "session": _status().session.model_copy(update={"integration_pr": 73}),
        }
    )
    target = PullRequestTarget(
        number=73,
        url="https://github.com/example/project/pull/73",
        state=PRState.OPEN,
        is_cross_repository=False,
        base_ref_name="main",
        base_ref_oid="1" * 40,
        head_ref_name="feature",
        head_ref_oid="2" * 40,
        changed_files=1,
        additions=2,
        deletions=3,
    )
    calls: list[object] = []

    class Resolver:
        def check_ready(self) -> None:
            calls.append("ready")

        def resolve_pr_target(self, selector: str) -> PullRequestTarget:
            calls.append(("resolve", selector))
            return target

    def fake_init(
        repo: Path,
        resolved: PullRequestTarget,
        *,
        slice_pr_prefix: str,
    ) -> StatusResult:
        calls.append(("init", repo, resolved, slice_pr_prefix))
        return status

    monkeypatch.setattr(cli_main, "_backend", lambda repo: Resolver())
    monkeypatch.setattr(
        cli_main,
        "_facade",
        SimpleNamespace(init_session_from_pr=fake_init),
    )

    result = CliRunner().invoke(
        cli_main.main,
        ["init", "--pr", "73", "--slice-prefix", "review"],
    )

    assert result.exit_code == 0
    assert calls == [
        "ready",
        ("resolve", "73"),
        ("init", Path.cwd(), target, "review"),
    ]
    assert "Source PR: #73" in result.output
    assert "Next: `git-paoding status --summary`" in result.output
    assert "Action-needed atoms:" not in result.output


@pytest.mark.unit
def test_init_replace_base_prints_backup_without_constructing_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    status = _status()
    backup = tmp_path / "main.backup.json"
    calls: list[object] = []

    def fake_replace(
        repo: Path,
        *,
        base: str | None,
        pr_target: PullRequestTarget | None,
        slice_pr_prefix: str,
    ) -> ReplaceResult:
        calls.append((repo, base, pr_target, slice_pr_prefix))
        return ReplaceResult(status=status, backup_path=backup)

    monkeypatch.setattr(
        cli_main,
        "_backend",
        lambda repo: (_ for _ in ()).throw(AssertionError("backend must not be constructed")),
    )
    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(replace_session=fake_replace))

    result = CliRunner().invoke(
        cli_main.main,
        ["init", "--replace", "--base", "target", "--slice-prefix", "review"],
    )

    assert result.exit_code == 0
    assert calls == [(Path.cwd(), "target", None, "review")]
    assert f"Previous session backed up to: {backup}" in result.output


@pytest.mark.unit
def test_init_replace_pr_resolves_once_and_passes_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = PullRequestTarget(
        number=73,
        url="https://github.com/example/project/pull/73",
        state=PRState.OPEN,
        is_cross_repository=False,
        base_ref_name="main",
        base_ref_oid="1" * 40,
        head_ref_name="feature",
        head_ref_oid="2" * 40,
        changed_files=1,
        additions=2,
        deletions=3,
    )
    calls: list[object] = []

    class Resolver:
        def check_ready(self) -> None:
            calls.append("ready")

        def resolve_pr_target(self, selector: str) -> PullRequestTarget:
            calls.append(("resolve", selector))
            return target

    def fake_replace(
        repo: Path,
        *,
        base: str | None,
        pr_target: PullRequestTarget | None,
        slice_pr_prefix: str,
    ) -> ReplaceResult:
        calls.append(("replace", repo, base, pr_target, slice_pr_prefix))
        return ReplaceResult(status=_status(), backup_path=tmp_path / "backup.json")

    monkeypatch.setattr(cli_main, "_backend", lambda repo: Resolver())
    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(replace_session=fake_replace))

    result = CliRunner().invoke(cli_main.main, ["init", "--replace", "--pr", "73"])

    assert result.exit_code == 0
    assert calls == [
        "ready",
        ("resolve", "73"),
        ("replace", Path.cwd(), None, target, "slice"),
    ]


@pytest.mark.unit
def test_slice_add_renders_delta_not_large_atom_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = [
        Atom(
            atom_id=f"atom-{index}",
            path=f"src/file_{index}.py",
            kind=AtomKind.MODIFY,
            base_start=index,
            base_len=1,
            final_start=index,
            final_len=1,
            content_hash=f"hash-{index}",
            state=AtomState.UNASSIGNED,
            preview=f"+change {index}",
        )
        for index in range(163)
    ]
    status = StatusResult(
        session=SessionSummary(
            canonical_branch="feature/large-change",
            base_oid="base-oid",
            last_final_oid="final-oid",
        ),
        slices=[
            SliceSummary(
                id="geometry",
                title="Mask geometry",
                status=SliceStatus.ACTIVE,
            )
        ],
        atoms=atoms,
        unassigned_count=len(atoms),
    )

    monkeypatch.setattr(facade_api, "add_slice", lambda *args, **kwargs: status)

    result = CliRunner().invoke(
        cli_main.main,
        ["slice", "add", "geometry", "--title", "Mask geometry"],
    )

    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "Added slice: geometry",
        "Title: Mask geometry",
        "Session: feature/large-change",
        "Slices: 1 active",
        "Action needed: 163 unassigned, 0 ambiguous",
        "Run `git-paoding status` to inspect atoms.",
    ]
    assert "src/file_0.py" not in result.output
    assert "src/file_162.py" not in result.output


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [False, True])
def test_publish_action_needed_exits_two_in_both_rendering_modes(
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    status = _status()

    def fake_backend(repo: Path) -> object:
        return object()

    def fake_publish(
        repo: Path,
        *,
        backend: object,
        remote: str = "origin",
        progress: object = None,
        network_timeout: float | None = 120.0,
    ) -> PublishResult:
        if callable(progress):
            progress(SimpleNamespace(message="Reconciling canonical diff"))
        return PublishResult(action_needed=True, status=status)

    monkeypatch.setattr(cli_main, "_backend", lambda repo, timeout=None: fake_backend(repo))
    monkeypatch.setattr(facade_api, "publish", fake_publish)
    arguments = ["publish", "--json"] if as_json else ["publish"]

    result = CliRunner().invoke(cli_main.main, arguments)

    assert result.exit_code == 2
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["contract_version"] == 0
        assert payload["action_needed"] is True
        assert payload["status"] == status.model_dump(mode="json")
        assert "Reconciling canonical diff" in result.stderr
    else:
        assert "Publish stopped before remote effects." in result.output
        assert "Action needed: 1 unassigned, 0 ambiguous" in result.output


@pytest.mark.unit
def test_publish_json_keeps_progress_and_trace_on_stderr_and_disables_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_backend(repo: Path, *, timeout: float | None) -> object:
        calls.append(("backend", timeout))
        return object()

    def fake_publish(
        repo: Path,
        *,
        backend: object,
        remote: str,
        progress: object,
        network_timeout: float | None,
    ) -> PublishResult:
        calls.append(("publish", remote, network_timeout))
        assert callable(progress)
        progress(ProgressEvent(PublishPhase.RECONCILE, "Reconciling canonical diff"))
        return PublishResult(action_needed=False)

    monkeypatch.setattr(cli_main, "_backend", fake_backend)
    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(publish=fake_publish))

    result = CliRunner().invoke(
        cli_main.main,
        ["publish", "--json", "--trace", "--network-timeout", "0"],
    )

    assert result.exit_code == 0
    assert PublishResult.model_validate_json(result.stdout) == PublishResult(action_needed=False)
    assert "Reconciling canonical diff" in result.stderr
    assert "Publish complete in" in result.stderr
    assert "Trace:" in result.stderr
    assert "git-local: 0 processes" in result.stderr
    assert calls == [("backend", None), ("publish", "origin", None)]


@pytest.mark.unit
def test_publish_quiet_suppresses_progress_but_keeps_json_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_publish(
        repo: Path,
        *,
        backend: object,
        remote: str,
        progress: object,
        network_timeout: float | None,
    ) -> PublishResult:
        assert progress is None
        return PublishResult(action_needed=False)

    monkeypatch.setattr(cli_main, "_backend", lambda repo, timeout: object())
    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(publish=fake_publish))

    result = CliRunner().invoke(cli_main.main, ["publish", "--json", "--quiet"])

    assert result.exit_code == 0
    assert PublishResult.model_validate_json(result.stdout) == PublishResult(action_needed=False)
    assert result.stderr == ""


@pytest.mark.unit
def test_operational_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_status(repo: Path) -> StatusResult:
        raise PaodingError("no session")

    monkeypatch.setattr(facade_api, "get_status", fail_status)

    result = CliRunner().invoke(cli_main.main, ["status"])

    assert result.exit_code == 1
    assert "Error: no session" in result.output


@pytest.mark.unit
def test_help_documents_public_exit_codes_and_complete_command_surface() -> None:
    result = CliRunner().invoke(cli_main.main, ["--help"])

    assert result.exit_code == 0
    assert "0 = success/clean" in result.output
    assert "2 = action needed" in result.output
    assert "1 = operational error" in result.output
    for command in ("agent", "archive", "assign", "focus", "publish", "slice", "status"):
        assert command in result.output


@pytest.mark.unit
def test_status_previews_default_to_three_lines_and_full_shows_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_status = _status()
    short_status.atoms[0].preview = "one\ntwo\nthree\n…"
    full_status = _status()
    full_status.atoms[0].preview = "one\ntwo\nthree\nfour\nfive"
    calls: list[bool] = []

    def fake_get_status(repo: Path, *, full: bool) -> StatusResult:
        calls.append(full)
        return full_status if full else short_status

    monkeypatch.setattr(
        cli_main,
        "_facade",
        SimpleNamespace(get_status=fake_get_status),
    )
    runner = CliRunner()

    short = runner.invoke(cli_main.main, ["status"])
    full = runner.invoke(cli_main.main, ["status", "--full"])

    assert short.exit_code == full.exit_code == 2
    assert "    three" in short.output
    assert "    …" in short.output
    assert "    four" not in short.output
    assert "    four" in full.output
    assert "    five" in full.output
    assert calls == [False, True]


@pytest.mark.unit
def test_interactive_assign_passes_force_and_preserves_delta_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_assign(
        repo: Path,
        slice_id: str,
        selectors: Sequence[str],
        *,
        force: bool,
    ) -> AssignResult:
        calls.append((repo, slice_id, tuple(selectors), force))
        return AssignResult(
            assigned=[
                AssignmentRecord(
                    atom_id="a1",
                    path="app.py",
                    previous_owner="old",
                    owner=slice_id,
                    preview="-old\n+new",
                )
            ]
        )

    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(assign=fake_assign))

    result = CliRunner().invoke(
        cli_main.main,
        ["assign", "review", "src", "app.py:10-20", "--force"],
    )

    assert result.exit_code == 0
    assert calls == [(Path.cwd(), "review", ("src", "app.py:10-20"), True)]
    assert "assigned a1 app.py -> review" in result.output
    assert "-old" in result.output


@pytest.mark.unit
@pytest.mark.parametrize("use_stdin", [False, True])
def test_status_json_can_feed_one_all_or_nothing_batch_assignment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    use_stdin: bool,
) -> None:
    status = StatusResult(
        session=SessionSummary(
            canonical_branch="feature/batch",
            base_oid="base-oid",
            last_final_oid="final-oid",
        ),
        slices=[
            SliceSummary(id="storage", title="Storage", status=SliceStatus.ACTIVE),
            SliceSummary(id="search", title="Search", status=SliceStatus.ACTIVE),
        ],
        atoms=[
            _status().atoms[0].model_copy(update={"atom_id": "a1", "path": "storage.py"}),
            _status().atoms[0].model_copy(update={"atom_id": "a2", "path": "search.py"}),
        ],
        unassigned_count=2,
    )
    assigned_results: list[AssignResult] = []

    def fake_batch(repo: Path, request: object) -> AssignResult:
        updated, assignment = assign_batch_selectors(
            status.atoms,
            assignments=request.assignments,  # type: ignore[attr-defined]
            active_slice_ids={"storage", "search"},
            force=request.force,  # type: ignore[attr-defined]
        )
        assert all(atom.owner is not None for atom in updated)
        assigned_results.append(assignment)
        return assignment

    monkeypatch.setattr(
        cli_main,
        "_facade",
        SimpleNamespace(
            get_status=lambda repo, full: status,
            assign_batch=fake_batch,
        ),
    )
    runner = CliRunner()
    status_result = runner.invoke(cli_main.main, ["status", "--json"])
    payload = json.loads(status_result.output)
    plan = {
        "contract_version": 0,
        "assignments": {
            "storage": [payload["atoms"][0]["atom_id"]],
            "search": [payload["atoms"][1]["atom_id"]],
        },
        "force": False,
    }
    plan_text = json.dumps(plan)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan_text, encoding="utf-8")
    arguments = ["assign", "--batch", "-" if use_stdin else str(plan_path)]

    assign_result = runner.invoke(
        cli_main.main,
        arguments,
        input=plan_text if use_stdin else None,
    )

    assert status_result.exit_code == 2
    assert assign_result.exit_code == 0
    assert len(assigned_results) == 1
    assert [record.atom_id for record in assigned_results[0].assigned] == ["a1", "a2"]
    assert "assigned a1 storage.py -> storage" in assign_result.output
    assert "assigned a2 search.py -> search" in assign_result.output


@pytest.mark.unit
def test_batch_cli_rejects_mixed_modes_before_facade_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    def fake_batch(repo: Path, request: object) -> AssignResult:
        nonlocal called
        called = True
        return AssignResult()

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "contract_version": 0,
                "assignments": {"review": ["deadbeef"]},
                "force": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_main, "_facade", SimpleNamespace(assign_batch=fake_batch))

    result = CliRunner().invoke(
        cli_main.main,
        ["assign", "review", "--batch", str(plan_path)],
    )

    assert result.exit_code == 1
    assert "cannot be combined" in result.output
    assert called is False


@pytest.mark.unit
def test_slice_crud_focus_and_archive_render_only_mutation_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _status().model_copy(
        update={
            "slices": [
                SliceSummary(id="review", title="Review", status=SliceStatus.ACTIVE),
            ]
        }
    )
    archived = active.model_copy(
        update={
            "slices": [
                SliceSummary(id="review", title="Review", status=SliceStatus.ARCHIVED),
            ]
        }
    )
    calls: list[tuple[object, ...]] = []

    def remove_slice(repo: Path, slice_id: str) -> StatusResult:
        calls.append(("remove", slice_id))
        return active.model_copy(update={"slices": []})

    def rename_slice(repo: Path, slice_id: str, title: str) -> StatusResult:
        calls.append(("rename", slice_id, title))
        return active

    def set_focus(repo: Path, slice_id: str | None) -> StatusResult:
        calls.append(("focus", slice_id))
        return active

    def archive(repo: Path, *, backend: object, remote: str) -> StatusResult:
        calls.append(("archive", remote, backend))
        return archived

    facade = SimpleNamespace(
        list_slices=lambda repo: active,
        remove_slice=remove_slice,
        rename_slice=rename_slice,
        set_focus=set_focus,
        archive=archive,
    )
    backend = object()
    monkeypatch.setattr(cli_main, "_facade", facade)
    monkeypatch.setattr(cli_main, "_backend", lambda repo: backend)
    runner = CliRunner()

    listed = runner.invoke(cli_main.main, ["slice", "list"])
    removed = runner.invoke(cli_main.main, ["slice", "remove", "review"])
    renamed = runner.invoke(
        cli_main.main,
        ["slice", "rename", "review", "--title", "Renamed"],
    )
    focused = runner.invoke(cli_main.main, ["focus", "review"])
    cleared = runner.invoke(cli_main.main, ["focus", "--clear"])
    archive_result = runner.invoke(cli_main.main, ["archive", "--remote", "upstream"])

    assert all(
        result.exit_code == 0
        for result in (listed, removed, renamed, focused, cleared, archive_result)
    )
    assert "review  active  0 files +0 -0" in listed.output
    assert "app.py" not in listed.output
    assert "Removed slice: review" in removed.output
    assert "atoms are now unassigned" in removed.output
    assert "Renamed slice: review" in renamed.output
    assert "Focus: review" in focused.output
    assert "Focus: cleared" in cleared.output
    assert "Archived session: main" in archive_result.output
    assert all("a1 app.py" not in result.output for result in (removed, renamed, focused, cleared))
    assert calls == [
        ("remove", "review"),
        ("rename", "review", "Renamed"),
        ("focus", "review"),
        ("focus", None),
        ("archive", "upstream", backend),
    ]
