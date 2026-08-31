"""Offline tests for the owner-authorized atomic-push probe."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from scripts import live_publish_validation as live  # noqa: E402

REMOTE = "https://github.com/example/git-paoding-live-publish-20260831.git"
OLD_OID = "1" * 40
NEW_OID = "2" * 40


def _refs() -> live.ProbeRefs:
    return live.make_probe_refs("20260831T120000Z", "abcdef0123456789")


@pytest.mark.unit
def test_private_scratch_target_requires_exact_identity_visibility_and_description() -> None:
    payload = {
        "nameWithOwner": "example/git-paoding-live-publish-20260831",
        "visibility": "PRIVATE",
        "isArchived": False,
        "description": live.PRIVATE_SCRATCH_DESCRIPTION,
    }

    live.validate_private_scratch_target(
        payload,
        expected_slug="example/git-paoding-live-publish-20260831",
    )

    for changed in (
        {"nameWithOwner": "example/production"},
        {"visibility": "PUBLIC"},
        {"isArchived": True},
        {"description": "unrelated private repository"},
    ):
        with pytest.raises(live.ValidationFailure):
            live.validate_private_scratch_target(
                {**payload, **changed},
                expected_slug="example/git-paoding-live-publish-20260831",
            )


@pytest.mark.unit
def test_probe_refs_use_one_unique_non_product_namespace() -> None:
    refs = _refs()

    assert refs.first.startswith(f"{live.PROBE_REF_ROOT}/")
    assert refs.second.startswith(f"{live.PROBE_REF_ROOT}/")
    assert refs.first.rsplit("/", maxsplit=1)[0] == refs.second.rsplit("/", maxsplit=1)[0]
    assert "/paoding/" not in refs.first

    with pytest.raises(live.ValidationFailure, match="outside"):
        live.build_exact_lease_delete(REMOTE, "refs/heads/main", OLD_OID)


@pytest.mark.unit
def test_atomic_create_push_targets_both_throwaway_refs_once() -> None:
    refs = _refs()

    plan = live.build_atomic_create_push(
        REMOTE,
        refs,
        first_oid=OLD_OID,
        second_oid=NEW_OID,
    )

    assert plan.args == (
        "git",
        "push",
        "--atomic",
        REMOTE,
        f"{OLD_OID}:{refs.first}",
        f"{NEW_OID}:{refs.second}",
    )
    assert plan.expected_oids == {refs.first: OLD_OID, refs.second: NEW_OID}

    with pytest.raises(live.ValidationFailure, match="invalid probe object OID"):
        live.build_atomic_create_push(
            REMOTE,
            refs,
            first_oid="HEAD",
            second_oid=NEW_OID,
        )


@pytest.mark.unit
def test_atomic_update_has_one_exact_lease_per_ref() -> None:
    refs = _refs()
    observed = {refs.first: OLD_OID, refs.second: NEW_OID}
    desired = {refs.first: NEW_OID, refs.second: OLD_OID}

    plan = live.build_atomic_lease_push(
        REMOTE,
        refs,
        observed_oids=observed,
        desired_oids=desired,
    )

    assert plan.args == (
        "git",
        "push",
        "--atomic",
        f"--force-with-lease={refs.first}:{OLD_OID}",
        f"--force-with-lease={refs.second}:{NEW_OID}",
        REMOTE,
        f"{NEW_OID}:{refs.first}",
        f"{OLD_OID}:{refs.second}",
    )
    assert plan.expected_oids == desired


@pytest.mark.unit
def test_cleanup_delete_is_guarded_by_the_observed_oid() -> None:
    ref = _refs().first

    assert live.build_exact_lease_delete(REMOTE, ref, NEW_OID) == (
        "git",
        "push",
        f"--force-with-lease={ref}:{NEW_OID}",
        REMOTE,
        f":{ref}",
    )


@pytest.mark.unit
def test_cleanup_reobserves_and_deletes_each_owned_ref_with_an_exact_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"
    probe = live.AtomicPushProbe(ROOT, evidence_path, "example/private-scratch")
    probe.refs = _refs()
    probe.target_validated = True
    probe.cleanup_eligible = True
    probe.allowed_oids = {OLD_OID, NEW_OID}
    observations = iter(
        [
            {probe.refs.first: OLD_OID, probe.refs.second: NEW_OID},
            {},
        ]
    )
    deletes: list[tuple[str, ...]] = []
    monkeypatch.setattr(probe, "_remote_oids", lambda operation: next(observations))

    def fake_run(operation: str, args: tuple[str, ...]) -> live.CommandResult:
        deletes.append(args)
        return live.CommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(probe, "_run", fake_run)

    probe._cleanup()

    assert probe.evidence["cleanup"]["status"] == "cleaned"
    assert deletes == [
        live.build_exact_lease_delete(probe.remote_url, probe.refs.first, OLD_OID),
        live.build_exact_lease_delete(probe.remote_url, probe.refs.second, NEW_OID),
    ]


@pytest.mark.unit
def test_cleanup_refuses_to_delete_a_probe_ref_changed_to_an_unknown_oid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"
    probe = live.AtomicPushProbe(ROOT, evidence_path, "example/private-scratch")
    probe.refs = _refs()
    probe.target_validated = True
    probe.cleanup_eligible = True
    probe.allowed_oids = {OLD_OID, NEW_OID}
    unknown_oid = "3" * 40
    observations = iter(
        [
            {probe.refs.first: unknown_oid},
            {probe.refs.first: unknown_oid},
        ]
    )
    monkeypatch.setattr(probe, "_remote_oids", lambda operation: next(observations))
    monkeypatch.setattr(
        probe,
        "_run",
        lambda operation, args: (_ for _ in ()).throw(
            AssertionError("unexpected deletion of an unrecognized OID")
        ),
    )

    probe._cleanup()

    cleanup = probe.evidence["cleanup"]
    assert cleanup["status"] == "failed"
    assert cleanup["refused_unexpected_oids"] == {probe.refs.first: unknown_oid}


@pytest.mark.unit
def test_remote_oid_parser_accepts_only_exact_probe_refs() -> None:
    refs = _refs()
    output = f"{OLD_OID}\t{refs.first}\n{NEW_OID}\t{refs.second}\n"

    assert live.parse_remote_oids(output, allowed_refs=refs.as_tuple()) == {
        refs.first: OLD_OID,
        refs.second: NEW_OID,
    }

    with pytest.raises(live.ValidationFailure, match="unexpected ref"):
        live.parse_remote_oids(
            output + f"{OLD_OID}\trefs/heads/main\n",
            allowed_refs=refs.as_tuple(),
        )


@pytest.mark.unit
def test_probe_runner_prints_and_records_only_sanitized_operation_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"
    assert not evidence_path.exists()
    probe = live.AtomicPushProbe(ROOT, evidence_path, "example/private-scratch")
    credentialed_url = "https://secret-token@github.com/example/private-scratch.git"

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=1,
            stdout=f"sensitive stdout {credentialed_url}",
            stderr=f"sensitive stderr {credentialed_url}",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = probe._run("git-probe-test", ("git", "push", credentialed_url))

    rendered = capsys.readouterr()
    records = json.dumps(probe.commands)
    assert result.returncode == 1
    assert rendered.out == "$ [git-probe-test]\n"
    assert rendered.err == ""
    assert "secret-token" not in rendered.out
    assert "secret-token" not in records
    assert probe.commands[0]["operation"] == "git-probe-test"
    assert probe.commands[0]["failure_category"] == "command-failed"


@pytest.mark.unit
def test_probe_success_requires_verified_swap_and_safe_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"
    probe = live.AtomicPushProbe(ROOT, evidence_path, "example/private-scratch")
    probe.refs = _refs()
    remote_rows = {
        "git-confirm-probe-namespace-absent": "",
        "git-observe-created-probe-refs": (
            f"{OLD_OID}\t{probe.refs.first}\n{NEW_OID}\t{probe.refs.second}\n"
        ),
        "git-verify-atomic-lease-update": (
            f"{NEW_OID}\t{probe.refs.first}\n{OLD_OID}\t{probe.refs.second}\n"
        ),
        "git-observe-probe-refs-for-cleanup": (
            f"{NEW_OID}\t{probe.refs.first}\n{OLD_OID}\t{probe.refs.second}\n"
        ),
        "git-verify-probe-ref-cleanup": "",
    }

    def fake_run(operation: str, args: tuple[str, ...]) -> live.CommandResult:
        if operation == "gh-validate-private-scratch-target":
            return live.CommandResult(
                stdout=json.dumps(
                    {
                        "nameWithOwner": "example/private-scratch",
                        "visibility": "PRIVATE",
                        "isArchived": False,
                        "description": live.PRIVATE_SCRATCH_DESCRIPTION,
                    }
                ),
                stderr="",
                returncode=0,
            )
        if operation == "git-resolve-probe-newer-oid":
            return live.CommandResult(stdout=NEW_OID, stderr="", returncode=0)
        if operation == "git-resolve-probe-older-oid":
            return live.CommandResult(stdout=OLD_OID, stderr="", returncode=0)
        if operation in remote_rows:
            return live.CommandResult(stdout=remote_rows[operation], stderr="", returncode=0)
        return live.CommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(probe, "_run", fake_run)
    monkeypatch.setattr(probe, "_write_evidence", lambda: None)

    probe.execute()

    assert probe.evidence["probe_result"] == "supported"
    assert probe.evidence["atomic_transport_supported"] is True
    assert probe.evidence["atomic_exact_lease_update_verified"] is True
    assert probe.evidence["fallback_constant_should_remain_off"] is True
    assert probe.evidence["cleanup"]["status"] == "cleaned"
    assert not evidence_path.exists()


@pytest.mark.unit
def test_atomic_rejection_still_runs_cleanup_and_records_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"
    probe = live.AtomicPushProbe(ROOT, evidence_path, "example/private-scratch")
    probe.refs = _refs()

    def fake_run(operation: str, args: tuple[str, ...]) -> live.CommandResult:
        if operation == "gh-validate-private-scratch-target":
            return live.CommandResult(
                stdout=json.dumps(
                    {
                        "nameWithOwner": "example/private-scratch",
                        "visibility": "PRIVATE",
                        "isArchived": False,
                        "description": live.PRIVATE_SCRATCH_DESCRIPTION,
                    }
                ),
                stderr="",
                returncode=0,
            )
        if operation == "git-resolve-probe-newer-oid":
            return live.CommandResult(stdout=NEW_OID, stderr="", returncode=0)
        if operation == "git-resolve-probe-older-oid":
            return live.CommandResult(stdout=OLD_OID, stderr="", returncode=0)
        if operation == "git-push-atomic-create-two-probe-refs":
            return live.CommandResult(
                stdout="",
                stderr="the receiving end does not support --atomic push",
                returncode=1,
            )
        return live.CommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(probe, "_run", fake_run)
    monkeypatch.setattr(probe, "_write_evidence", lambda: None)

    with pytest.raises(live.ValidationFailure, match="atomic-not-supported"):
        probe.execute()

    assert probe.evidence["probe_result"] == "failed"
    assert probe.evidence["atomic_transport_supported"] is False
    assert probe.evidence["fallback_constant_should_remain_off"] is False
    assert probe.evidence["cleanup"]["status"] == "cleaned"
    assert not evidence_path.exists()


@pytest.mark.unit
def test_release_trace_parser_extracts_phases_and_process_counts() -> None:
    stderr = """Reconciling canonical diff
Trace:
  reconcile: 0.012s
  validate-github: 0.340s
  load-context: 0.125s
  build-projection: 0.500s
  sync-refs: 0.750s
  slice-pr: 1.000s
  integration-index: 0.400s
  persist: 0.010s
  git-local: 54 processes, 0.200s
  git-remote: 2 processes, 0.600s
  gh-read: 3 processes, 0.300s
  gh-write: 9 processes, 1.100s
"""

    phases, processes = live.parse_publish_trace(stderr)

    assert phases == {
        "reconcile": 0.012,
        "validate-github": 0.34,
        "load-context": 0.125,
        "build-projection": 0.5,
        "sync-refs": 0.75,
        "slice-pr": 1.0,
        "integration-index": 0.4,
        "persist": 0.01,
    }
    assert processes == {"git-local": 54, "git-remote": 2, "gh-read": 3, "gh-write": 9}


@pytest.mark.unit
def test_release_progress_observations_measure_first_line_and_longest_gap() -> None:
    result = live.TimedCommandResult(
        stdout="{}",
        stderr="Reconciling canonical diff\nLoading shared projection context\nTrace:\n",
        returncode=0,
        duration_seconds=2.0,
        stderr_events=(
            (0.2, "Reconciling canonical diff"),
            (0.8, "Loading shared projection context"),
            (1.9, "Trace:"),
        ),
    )

    observations = live.progress_observations(result)

    assert observations["first_progress_seconds"] == 0.2
    assert observations["first_progress_within_one_second"] is True
    assert observations["progress_event_count"] == 2
    assert observations["longest_silent_interval"] == {
        "seconds": 1.2,
        "after": "Loading shared projection context",
        "before": "command-end",
    }
    assert observations["no_unexplained_silent_interval"] is True


@pytest.mark.unit
def test_release_validation_parser_requires_explicit_mode_inputs() -> None:
    args = live.parser().parse_args(
        [
            "--evidence",
            "docs/evidence/live-release.json",
            "--release-validation",
            "--release-repo",
            "example/private-scratch",
            "--baseline-pre-pr-seconds",
            "12.5",
            "--baseline-pre-pr-seconds",
            "13.5",
        ]
    )

    assert args.release_validation is True
    assert args.release_repo == "example/private-scratch"
    assert args.baseline_pre_pr_seconds == [12.5, 13.5]


@pytest.mark.unit
def test_probe_evidence_must_be_a_new_json_file_in_docs_evidence() -> None:
    accepted = ROOT / "docs" / "evidence" / "__offline-unit-test-no-write__.json"

    assert live.validate_probe_evidence_path(ROOT, accepted) == accepted
    with pytest.raises(live.ValidationFailure, match="under docs/evidence"):
        live.validate_probe_evidence_path(ROOT, ROOT / "probe.json")
    with pytest.raises(live.ValidationFailure, match="JSON"):
        live.validate_probe_evidence_path(
            ROOT,
            ROOT / "docs" / "evidence" / "probe.txt",
        )


@pytest.mark.unit
def test_atomic_unsupported_diagnostic_is_classified_without_retaining_it() -> None:
    assert (
        live.atomic_failure_category("fatal: the receiving end does not support --atomic push")
        == "atomic-not-supported"
    )
    assert live.atomic_failure_category("authentication failed") == "command-failed"
