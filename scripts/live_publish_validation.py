#!/usr/bin/env python3
"""Run live publishing validation or an isolated atomic-push capability probe.

This is a manual, networked workflow. It is intentionally excluded from pytest/CI.
The full workflow creates and preserves a private repository and its Draft PRs. The
atomic-push mode requires an existing private validation repository and cleans up its
unique throwaway refs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, Sequence, cast

from git_paoding.store.jsonstore import branch_key

if __package__:
    from scripts.field_shape import (
        ATOM_COUNT,
        CHANGED_FILE_COUNT,
        DIRECTORY_COUNT,
        build_field_shape,
    )
else:
    from field_shape import (  # type: ignore[import-not-found,no-redef]
        ATOM_COUNT,
        CHANGED_FILE_COUNT,
        DIRECTORY_COUNT,
        build_field_shape,
    )

CANONICAL_BRANCH = "feature/live-publish-validation"
SLICE_A = "review"
SLICE_B = "context"
EMPTY_SLICE = "empty-check"
COMMENT_BODY = (
    "This line belongs to the primary review slice and should remain anchored after an "
    "unrelated same-file slice is published."
)

BASE_CONTENT = """# git-paoding live publish scenario
alpha = "base"
stable_01 = true
stable_02 = true
stable_03 = true
stable_04 = true
stable_05 = true
stable_06 = true
stable_07 = true
stable_08 = true
beta = "base"
"""

PROBE_REF_ROOT = "refs/heads/git-paoding-probes/atomic-push"
PRIVATE_SCRATCH_DESCRIPTION = (
    "Disposable-but-preserved git-paoding live publish validation evidence"
)
_REPO_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OPERATION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TRACE_PHASE_PATTERN = re.compile(r"^  ([a-z-]+): ([0-9]+(?:\.[0-9]+)?)s$")
_TRACE_PROCESS_PATTERN = re.compile(
    r"^  (git-local|git-remote|gh-read|gh-write): ([0-9]+) processes, "
    r"([0-9]+(?:\.[0-9]+)?)s$"
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True, slots=True)
class TimedCommandResult(CommandResult):
    """Captured command output plus observation times for stderr lines."""

    duration_seconds: float
    stderr_events: tuple[tuple[float, str], ...]


@dataclass(frozen=True, slots=True)
class ProbeRefs:
    """Unique remote refs used only by one atomic-push probe."""

    first: str
    second: str

    def as_tuple(self) -> tuple[str, str]:
        return self.first, self.second


@dataclass(frozen=True, slots=True)
class ProbePushPlan:
    """Pure description of one two-ref push and its expected remote result."""

    args: tuple[str, ...]
    expected_oids: dict[str, str]


class ValidationFailure(RuntimeError):
    """Raised when a live observation violates the publish workflow contract."""


def fail(message: str) -> NoReturn:
    raise ValidationFailure(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def run(
    args: list[str],
    *,
    cwd: Path,
    expected: int | tuple[int, ...] = 0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    expected_codes = (expected,) if isinstance(expected, int) else expected
    print(f"$ {shlex.join(args)}", flush=True)
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode not in expected_codes:
        fail(
            f"command exited {completed.returncode}, expected {expected_codes}: "
            f"{shlex.join(args)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return CommandResult(completed.stdout, completed.stderr, completed.returncode)


def run_timed(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> TimedCommandResult:
    """Capture both streams concurrently and timestamp each non-empty stderr line."""

    print(f"$ {shlex.join(args)}", flush=True)
    started = time.perf_counter()
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    require(process.stdout is not None and process.stderr is not None, "missing command pipe")
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stderr_events: list[tuple[float, str]] = []
    event_lock = threading.Lock()

    def read_stdout() -> None:
        assert process.stdout is not None
        stdout_lines.extend(process.stdout.readlines())

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line)
            rendered = line.rstrip("\r\n")
            if rendered:
                with event_lock:
                    stderr_events.append((time.perf_counter() - started, rendered))

    readers = (
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    )
    for reader in readers:
        reader.start()
    returncode = process.wait()
    for reader in readers:
        reader.join()
    duration = time.perf_counter() - started
    return TimedCommandResult(
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        returncode=returncode,
        duration_seconds=duration,
        stderr_events=tuple(stderr_events),
    )


def parse_publish_trace(stderr: str) -> tuple[dict[str, float], dict[str, int]]:
    """Parse the CLI's stable aggregate trace without retaining command details."""

    phases: dict[str, float] = {}
    processes: dict[str, int] = {}
    for line in stderr.splitlines():
        phase_match = _TRACE_PHASE_PATTERN.fullmatch(line)
        if phase_match:
            phases[phase_match.group(1)] = float(phase_match.group(2))
            continue
        process_match = _TRACE_PROCESS_PATTERN.fullmatch(line)
        if process_match:
            processes[process_match.group(1)] = int(process_match.group(2))
    return phases, processes


def progress_observations(result: TimedCommandResult) -> dict[str, Any]:
    """Summarize first-progress latency and gaps between visible progress events."""

    progress_events = [
        (seconds, line)
        for seconds, line in result.stderr_events
        if not line.startswith(("Trace:", "  ", "Publish complete in "))
    ]
    first_progress = next(
        (
            (seconds, line)
            for seconds, line in progress_events
            if line == "Reconciling canonical diff"
        ),
        None,
    )
    points = [(0.0, "command-start"), *progress_events, (result.duration_seconds, "command-end")]
    gaps = [
        {
            "seconds": round(current[0] - previous[0], 6),
            "after": previous[1],
            "before": current[1],
        }
        for previous, current in zip(points, points[1:])
    ]
    longest = max(gaps, key=lambda item: cast(float, item["seconds"]))
    return {
        "first_progress_seconds": (
            round(first_progress[0], 6) if first_progress is not None else None
        ),
        "first_progress_line": first_progress[1] if first_progress is not None else None,
        "progress_event_count": len(progress_events),
        "longest_silent_interval": longest,
        "first_progress_within_one_second": (
            first_progress is not None and first_progress[0] <= 1.0
        ),
        "no_unexplained_silent_interval": cast(float, longest["seconds"]) <= 5.0,
    }


def parse_json(result: CommandResult, *, context: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"{context} returned invalid JSON: {error}\n{result.stdout}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_repo_slug(repo_slug: str) -> str:
    """Accept only an explicit GitHub owner/name pair safe for URL construction."""

    if not _REPO_SLUG_PATTERN.fullmatch(repo_slug):
        fail("probe repository must be an explicit GitHub OWNER/NAME slug")
    return repo_slug


def validate_oid(oid: str) -> str:
    """Accept Git SHA-1 or SHA-256 object names without revision syntax."""

    require(bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)), "invalid probe object OID")
    return oid


def validate_private_scratch_target(payload: object, *, expected_slug: str) -> None:
    """Refuse any repository except the explicitly named private validation target."""

    require(isinstance(payload, dict), "probe repository metadata was not an object")
    metadata = cast(dict[str, object], payload)
    require(metadata.get("nameWithOwner") == expected_slug, "probe repository identity mismatch")
    require(metadata.get("visibility") == "PRIVATE", "probe repository is not private")
    require(metadata.get("isArchived") is False, "probe repository is archived")
    require(
        metadata.get("description") == PRIVATE_SCRATCH_DESCRIPTION,
        "probe repository does not have the live-validation scratch description",
    )


def make_probe_refs(timestamp: str, nonce: str) -> ProbeRefs:
    """Build a collision-resistant namespace disjoint from product-generated refs."""

    safe_timestamp = re.sub(r"[^0-9A-Za-z-]", "-", timestamp).strip("-")
    require(bool(safe_timestamp), "probe timestamp did not contain a safe character")
    require(bool(re.fullmatch(r"[0-9a-f]+", nonce)), "probe nonce must be lowercase hex")
    namespace = f"{PROBE_REF_ROOT}/{safe_timestamp}-{nonce}"
    refs = ProbeRefs(first=f"{namespace}/first", second=f"{namespace}/second")
    validate_probe_refs(refs)
    return refs


def validate_probe_refs(refs: ProbeRefs) -> None:
    """Keep cleanup and push targets inside the dedicated throwaway namespace."""

    require(refs.first != refs.second, "probe refs must be distinct")
    require(
        refs.first.rsplit("/", maxsplit=1)[0] == refs.second.rsplit("/", maxsplit=1)[0],
        "probe refs must share one unique namespace",
    )
    for ref in refs.as_tuple():
        validate_probe_ref(ref)


def validate_probe_ref(ref: str) -> None:
    """Reject deletion or update targets outside the probe namespace."""

    prefix = f"{PROBE_REF_ROOT}/"
    require(ref.startswith(prefix), f"unsafe probe ref outside {prefix}")
    require(ref.endswith(("/first", "/second")), "probe ref has an unexpected suffix")
    require("/paoding/" not in ref, "probe ref overlaps product-generated refs")


def build_atomic_create_push(
    remote_url: str,
    refs: ProbeRefs,
    *,
    first_oid: str,
    second_oid: str,
) -> ProbePushPlan:
    """Build the first atomic creation of both probe refs."""

    validate_probe_refs(refs)
    validate_oid(first_oid)
    validate_oid(second_oid)
    return ProbePushPlan(
        args=(
            "git",
            "push",
            "--atomic",
            remote_url,
            f"{first_oid}:{refs.first}",
            f"{second_oid}:{refs.second}",
        ),
        expected_oids={refs.first: first_oid, refs.second: second_oid},
    )


def build_atomic_lease_push(
    remote_url: str,
    refs: ProbeRefs,
    *,
    observed_oids: dict[str, str],
    desired_oids: dict[str, str],
) -> ProbePushPlan:
    """Build a two-ref atomic update with one exact lease per destination."""

    validate_probe_refs(refs)
    require(set(observed_oids) == set(refs.as_tuple()), "observed OIDs do not cover both refs")
    require(set(desired_oids) == set(refs.as_tuple()), "desired OIDs do not cover both refs")
    for oid in (*observed_oids.values(), *desired_oids.values()):
        validate_oid(oid)
    lease_args = tuple(f"--force-with-lease={ref}:{observed_oids[ref]}" for ref in refs.as_tuple())
    refspecs = tuple(f"{desired_oids[ref]}:{ref}" for ref in refs.as_tuple())
    return ProbePushPlan(
        args=("git", "push", "--atomic", *lease_args, remote_url, *refspecs),
        expected_oids=dict(desired_oids),
    )


def build_exact_lease_delete(remote_url: str, ref: str, observed_oid: str) -> tuple[str, ...]:
    """Build a deletion that cannot remove a ref changed by another actor."""

    validate_probe_ref(ref)
    validate_oid(observed_oid)
    return (
        "git",
        "push",
        f"--force-with-lease={ref}:{observed_oid}",
        remote_url,
        f":{ref}",
    )


def parse_remote_oids(output: str, *, allowed_refs: Sequence[str]) -> dict[str, str]:
    """Parse exact ls-remote rows while rejecting duplicates and unrelated refs."""

    allowed = set(allowed_refs)
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        cells = line.split("\t", maxsplit=1)
        require(len(cells) == 2, "probe ls-remote returned a malformed row")
        oid, ref = cells
        require(ref in allowed, f"probe ls-remote returned unexpected ref {ref!r}")
        require(ref not in parsed, f"probe ls-remote returned duplicate ref {ref!r}")
        parsed[ref] = validate_oid(oid)
    return parsed


def atomic_failure_category(stderr: str) -> str:
    """Classify atomic capability failures without retaining sensitive diagnostics."""

    normalized = stderr.casefold()
    if "does not support --atomic" in normalized or "atomic push is not supported" in normalized:
        return "atomic-not-supported"
    return "command-failed"


def validate_probe_evidence_path(source: Path, evidence_path: Path) -> Path:
    """Require live probe evidence to be a new JSON file under docs/evidence."""

    evidence_root = (source / "docs" / "evidence").resolve()
    resolved = evidence_path.resolve()
    require(resolved.parent == evidence_root, "probe evidence must be written under docs/evidence")
    require(resolved.suffix == ".json", "probe evidence must be a JSON file")
    require(not resolved.exists(), "probe evidence path already exists")
    return resolved


class AtomicPushProbe:
    """Run a self-cleaning two-ref atomic and exact-lease capability probe."""

    def __init__(self, source: Path, evidence_path: Path, repo_slug: str) -> None:
        self.source = source.resolve()
        self.evidence_path = validate_probe_evidence_path(self.source, evidence_path)
        self.repo_slug = validate_repo_slug(repo_slug)
        self.remote_url = f"https://github.com/{self.repo_slug}.git"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.refs = make_probe_refs(timestamp, secrets.token_hex(8))
        self.commands: list[dict[str, Any]] = []
        self.target_validated = False
        self.cleanup_eligible = False
        self.allowed_oids: set[str] = set()
        self.evidence: dict[str, Any] = {
            "evidence_version": 1,
            "scenario": "atomic two-ref exact-lease push capability probe",
            "started_at": utc_now(),
            "scratch_repo": self.repo_slug,
            "probe_refs": list(self.refs.as_tuple()),
            "commands": self.commands,
            "atomic_transport_supported": None,
            "atomic_exact_lease_update_verified": False,
            "probe_result": "pending",
            "fallback_constant_should_remain_off": False,
            "fallback_constant_changed": False,
        }

    def _run(self, operation: str, args: Sequence[str]) -> CommandResult:
        require(
            bool(_OPERATION_PATTERN.fullmatch(operation)),
            "probe operation name must be a sanitized kebab-case identifier",
        )
        print(f"$ [{operation}]", flush=True)
        started_at = utc_now()
        started = time.perf_counter()
        os_error = False
        command_env = os.environ.copy()
        command_env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                args,
                cwd=self.source,
                env=command_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            result = CommandResult(
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
            )
        except OSError:
            os_error = True
            result = CommandResult(stdout="", stderr="", returncode=127)
        self.commands.append(
            {
                "operation": operation,
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_seconds": round(time.perf_counter() - started, 6),
                "returncode": result.returncode,
                "failure_category": (
                    "executable-unavailable"
                    if os_error
                    else atomic_failure_category(result.stderr)
                    if result.returncode != 0
                    else None
                ),
            }
        )
        return result

    def _require_success(self, operation: str, result: CommandResult) -> None:
        if result.returncode != 0:
            category = atomic_failure_category(result.stderr)
            fail(f"probe operation {operation!r} failed ({category})")

    def _remote_oids(self, operation: str) -> dict[str, str]:
        result = self._run(
            operation,
            ("git", "ls-remote", self.remote_url, *self.refs.as_tuple()),
        )
        self._require_success(operation, result)
        return parse_remote_oids(result.stdout, allowed_refs=self.refs.as_tuple())

    def _validate_target(self) -> None:
        operation = "gh-validate-private-scratch-target"
        result = self._run(
            operation,
            (
                "gh",
                "repo",
                "view",
                self.repo_slug,
                "--json",
                "nameWithOwner,visibility,isArchived,description",
            ),
        )
        self._require_success(operation, result)
        try:
            payload: object = json.loads(result.stdout)
        except json.JSONDecodeError:
            fail("private scratch target lookup returned invalid JSON")
        validate_private_scratch_target(payload, expected_slug=self.repo_slug)
        self.target_validated = True
        self.evidence["scratch_repo_visibility"] = "private"
        self.evidence["scratch_target_validated"] = True

    def _resolve_source_oids(self) -> tuple[str, str]:
        newer_result = self._run(
            "git-resolve-probe-newer-oid",
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
        )
        self._require_success("git-resolve-probe-newer-oid", newer_result)
        older_result = self._run(
            "git-resolve-probe-older-oid",
            ("git", "rev-parse", "--verify", "HEAD^1^{commit}"),
        )
        self._require_success("git-resolve-probe-older-oid", older_result)
        newer_oid = newer_result.stdout.strip()
        older_oid = older_result.stdout.strip()
        for oid in (newer_oid, older_oid):
            validate_oid(oid)
        require(newer_oid != older_oid, "probe requires two distinct source commit OIDs")
        self.allowed_oids = {newer_oid, older_oid}
        self.evidence["source_commit"] = newer_oid
        self.evidence["source_parent_commit"] = older_oid
        return older_oid, newer_oid

    def _cleanup(self) -> None:
        should_attempt = self.target_validated and self.cleanup_eligible
        cleanup: dict[str, Any] = {
            "started_at": utc_now(),
            "attempted": should_attempt,
            "status": "not-needed" if not should_attempt else "pending",
        }
        self.evidence["cleanup"] = cleanup
        if not should_attempt:
            cleanup["completed_at"] = utc_now()
            return
        try:
            before = self._remote_oids("git-observe-probe-refs-for-cleanup")
            cleanup["observed_oids"] = before
            refused: dict[str, str] = {}
            failed: list[str] = []
            for ref, oid in before.items():
                if oid not in self.allowed_oids:
                    refused[ref] = oid
                    continue
                result = self._run(
                    "git-delete-probe-ref-with-exact-lease",
                    build_exact_lease_delete(self.remote_url, ref, oid),
                )
                if result.returncode != 0:
                    failed.append(ref)
            after = self._remote_oids("git-verify-probe-ref-cleanup")
            cleanup["final_remote_oids"] = after
            cleanup["refused_unexpected_oids"] = refused
            cleanup["failed_refs"] = failed
            cleanup["status"] = "cleaned" if not after and not refused and not failed else "failed"
        except ValidationFailure as error:
            cleanup["status"] = "unknown"
            cleanup["failure"] = str(error)
        finally:
            cleanup["completed_at"] = utc_now()

    def _write_evidence(self) -> None:
        self.evidence["completed_at"] = utc_now()
        self.evidence_path.write_text(
            json.dumps(self.evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Evidence: {self.evidence_path}")

    def execute(self) -> None:
        failure: ValidationFailure | None = None
        try:
            self._validate_target()
            older_oid, newer_oid = self._resolve_source_oids()
            before = self._remote_oids("git-confirm-probe-namespace-absent")
            require(not before, "unique probe namespace unexpectedly already exists")

            initial = build_atomic_create_push(
                self.remote_url,
                self.refs,
                first_oid=older_oid,
                second_oid=newer_oid,
            )
            self.evidence["initial_desired_oids"] = initial.expected_oids
            self.cleanup_eligible = True
            initial_result = self._run("git-push-atomic-create-two-probe-refs", initial.args)
            if initial_result.returncode != 0:
                category = atomic_failure_category(initial_result.stderr)
                self.evidence["atomic_transport_supported"] = (
                    False if category == "atomic-not-supported" else None
                )
                fail(f"initial atomic push failed ({category})")
            self.evidence["atomic_transport_supported"] = True

            observed = self._remote_oids("git-observe-created-probe-refs")
            self.evidence["observed_oids"] = observed
            require(
                observed == initial.expected_oids, "initial atomic push produced unexpected OIDs"
            )

            desired = {self.refs.first: newer_oid, self.refs.second: older_oid}
            lease_update = build_atomic_lease_push(
                self.remote_url,
                self.refs,
                observed_oids=observed,
                desired_oids=desired,
            )
            self.evidence["lease_desired_oids"] = desired
            lease_result = self._run(
                "git-push-atomic-update-with-exact-leases",
                lease_update.args,
            )
            self._require_success("git-push-atomic-update-with-exact-leases", lease_result)
            final_oids = self._remote_oids("git-verify-atomic-lease-update")
            self.evidence["remote_final_oids"] = final_oids
            require(final_oids == desired, "atomic exact-lease push produced unexpected OIDs")
            self.evidence["atomic_exact_lease_update_verified"] = True
        except ValidationFailure as error:
            failure = error
            self.evidence["failure"] = str(error)
        finally:
            self._cleanup()
            cleanup_succeeded = self.evidence["cleanup"]["status"] == "cleaned"
            if failure is None and not cleanup_succeeded:
                failure = ValidationFailure("probe cleanup did not complete safely")
                self.evidence["failure"] = str(failure)
            probe_succeeded = (
                failure is None
                and self.evidence["atomic_exact_lease_update_verified"] is True
                and cleanup_succeeded
            )
            self.evidence["probe_result"] = "supported" if probe_succeeded else "failed"
            self.evidence["fallback_constant_should_remain_off"] = probe_succeeded
            self._write_evidence()
        if failure is not None:
            raise failure


class ReleaseValidation:
    """Validate the field-shaped release flow in one owner-approved scratch repository."""

    def __init__(
        self,
        source: Path,
        evidence_path: Path,
        repo_slug: str,
        baseline_pre_pr_seconds: Sequence[float],
    ) -> None:
        self.source = source.resolve()
        self.evidence_path = validate_probe_evidence_path(self.source, evidence_path)
        self.repo_slug = validate_repo_slug(repo_slug)
        self.remote_url = f"https://github.com/{self.repo_slug}.git"
        self.baseline_pre_pr_seconds = tuple(baseline_pre_pr_seconds)
        require(
            all(value > 0 for value in self.baseline_pre_pr_seconds),
            "pre-PR baseline samples must be positive",
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.base_branch = f"git-paoding-release-base-{timestamp}"
        self.canonical_branch = f"git-paoding-release-field-{timestamp}"
        self.work_root = Path(tempfile.mkdtemp(prefix="git-paoding-release-validation-"))
        self.repo = self.work_root / "repo"
        self.command_env = os.environ.copy()
        self.command_env["GIT_TERMINAL_PROMPT"] = "0"
        source_path = str(self.source / "src")
        inherited_pythonpath = self.command_env.get("PYTHONPATH")
        self.command_env["PYTHONPATH"] = (
            f"{source_path}{os.pathsep}{inherited_pythonpath}"
            if inherited_pythonpath
            else source_path
        )
        self.evidence: dict[str, Any] = {
            "evidence_version": 1,
            "scenario": "v0.1.2 field-shaped publish and init-from-PR validation",
            "started_at": utc_now(),
            "scratch_repo": self.repo_slug,
            "field_shape": {
                "directories": DIRECTORY_COUNT,
                "changed_files": CHANGED_FILE_COUNT,
                "atoms": ATOM_COUNT,
                "slices": 7,
            },
            "atomic_push_evidence": "docs/evidence/atomic-push-github-2026-08-31.json",
            "fallback_constant_remains_off": True,
            "generated_refs": [
                f"refs/heads/paoding/{branch_key(self.canonical_branch)}/slice-{index}/{side}"
                for index in range(1, 8)
                for side in ("base", "head")
            ],
        }

    def git(self, *args: str, expected: int | tuple[int, ...] = 0) -> CommandResult:
        return run(["git", *args], cwd=self.repo, expected=expected, env=self.command_env)

    def gh_source(self, *args: str) -> CommandResult:
        return run(["gh", *args], cwd=self.source, env=self.command_env)

    def _paoding_command(self, *args: str) -> list[str]:
        return [
            sys.executable,
            "-c",
            "from git_paoding.cli.main import main; main()",
            "--",
            *args,
        ]

    def paoding(self, *args: str, expected: int | tuple[int, ...] = 0) -> CommandResult:
        return run(
            self._paoding_command(*args),
            cwd=self.repo,
            expected=expected,
            env=self.command_env,
        )

    def paoding_timed(self, *args: str) -> TimedCommandResult:
        result = run_timed(
            self._paoding_command(*args),
            cwd=self.repo,
            env=self.command_env,
        )
        if result.returncode != 0:
            fail(
                f"timed git-paoding command exited {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def _validate_target(self) -> None:
        payload = parse_json(
            self.gh_source(
                "repo",
                "view",
                self.repo_slug,
                "--json",
                "nameWithOwner,visibility,isArchived,description",
            ),
            context="gh repo view release-validation target",
        )
        validate_private_scratch_target(payload, expected_slug=self.repo_slug)

    def _materialize(self, state: dict[str, str]) -> None:
        for relative_path, content in state.items():
            destination = self.repo / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

    def _seed_field_shape(self) -> None:
        shape = build_field_shape()
        self.repo.mkdir()
        self.git("init", "--quiet", f"--initial-branch={self.base_branch}")
        self.git("config", "user.name", "git-paoding release validation")
        self.git("config", "user.email", "git-paoding@localhost")
        self.git("remote", "add", "origin", self.remote_url)
        self._materialize(shape.base)
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Field-shaped validation base")
        self.git("push", "--quiet", "--set-upstream", "origin", self.base_branch)
        self.git("switch", "--quiet", "-c", self.canonical_branch)
        self._materialize(shape.final)
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Field-shaped validation final")
        self.git("push", "--quiet", "--set-upstream", "origin", self.canonical_branch)
        self.evidence["base_branch"] = self.base_branch
        self.evidence["canonical_branch"] = self.canonical_branch
        self.evidence["base_oid"] = self.git(
            "rev-parse", f"{self.base_branch}^{{commit}}"
        ).stdout.strip()
        self.evidence["final_oid"] = self.git("rev-parse", "HEAD^{commit}").stdout.strip()

    def _create_integration_pr_and_init(self) -> None:
        created = self.gh_source(
            "pr",
            "create",
            "--repo",
            self.repo_slug,
            "--draft",
            "--base",
            self.base_branch,
            "--head",
            self.canonical_branch,
            "--title",
            "git-paoding v0.1.2 field-shaped validation",
            "--body",
            "Owner-authorized scratch integration PR for git-paoding release validation.",
        )
        pr_url = created.stdout.strip()
        require(
            pr_url.startswith(f"https://github.com/{self.repo_slug}/pull/"), "unexpected PR URL"
        )
        init_result = self.paoding("init", "--pr", pr_url)
        self.evidence["init_pr_smoke"] = {
            "pr_url": pr_url,
            "exit_code": init_result.returncode,
            "open_pr": True,
            "passed": True,
        }

    def _configure_slices(self) -> None:
        shape = build_field_shape()
        for slice_id in shape.slice_paths:
            self.paoding("slice", "add", slice_id, "--title", f"Field review {slice_id}")
        assignment_path = self.work_root / "assignments.json"
        assignment_path.write_text(
            json.dumps(
                {
                    "contract_version": 0,
                    "assignments": {
                        slice_id: list(paths) for slice_id, paths in shape.slice_paths.items()
                    },
                    "force": False,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.paoding("assign", "--batch", str(assignment_path), "--quiet", "--json")
        status = cast(
            dict[str, Any],
            parse_json(
                self.paoding("status", "--summary", "--json"),
                context="field-shaped status summary",
            ),
        )
        require(status["total_atom_count"] == ATOM_COUNT, "field-shaped atom count changed")
        require(status["unassigned_count"] == 0, "field-shaped assignments are incomplete")

    def _pr_snapshot_hash(self) -> str:
        payload = parse_json(
            self.gh_source(
                "pr",
                "list",
                "--repo",
                self.repo_slug,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,updatedAt,title,body,headRefOid,baseRefOid",
            ),
            context="open PR snapshot",
        )
        return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _publish_record(self, result: TimedCommandResult) -> dict[str, Any]:
        payload = cast(dict[str, Any], json.loads(result.stdout))
        phases, processes = parse_publish_trace(result.stderr)
        require(len(phases) == 8, "publish trace did not report all eight phases")
        require(
            set(processes) == {"git-local", "git-remote", "gh-read", "gh-write"},
            "publish trace process categories are incomplete",
        )
        pre_pr_phases = (
            "reconcile",
            "validate-github",
            "load-context",
            "build-projection",
            "sync-refs",
        )
        return {
            "duration_seconds": round(result.duration_seconds, 6),
            "phase_seconds": phases,
            "process_counts": processes,
            "pre_pr_preparation_seconds": round(sum(phases[name] for name in pre_pr_phases), 6),
            "progress": progress_observations(result),
            "publish_payload": payload,
        }

    def _write_evidence(self) -> None:
        self.evidence["completed_at"] = utc_now()
        self.evidence["scratch_repo_retention"] = "preserved for owner audit"
        self.evidence_path.write_text(
            json.dumps(self.evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Evidence: {self.evidence_path}")

    def execute(self) -> None:
        for executable in ("git", "gh"):
            require(
                shutil.which(executable) is not None, f"missing required executable: {executable}"
            )
        self._validate_target()
        self._seed_field_shape()
        self._create_integration_pr_and_init()
        self._configure_slices()

        first = self._publish_record(
            self.paoding_timed("publish", "--json", "--trace", "--network-timeout", "120")
        )
        first_payload = cast(dict[str, Any], first["publish_payload"])
        require(first_payload["action_needed"] is False, "first field publish needs action")
        require(
            all(item["outcome"] == "created" for item in first_payload["slices"]),
            "first field publish did not create every slice PR",
        )
        before_no_op = self._pr_snapshot_hash()
        no_op = self._publish_record(
            self.paoding_timed("publish", "--json", "--trace", "--network-timeout", "120")
        )
        after_no_op = self._pr_snapshot_hash()
        no_op_payload = cast(dict[str, Any], no_op["publish_payload"])
        no_op_processes = cast(dict[str, int], no_op["process_counts"])
        require(
            all(item["outcome"] == "no-op" for item in no_op_payload["slices"]),
            "unchanged field republish was not a full no-op",
        )
        require(no_op_processes["git-remote"] == 1, "no-op republish performed a push")
        require(no_op_processes["gh-write"] == 0, "no-op republish edited a PR")
        require(before_no_op == after_no_op, "no-op republish changed an open PR snapshot")
        no_op["zero_pushes"] = True
        no_op["zero_pr_edits"] = True
        no_op["pr_snapshot_sha256_before"] = before_no_op
        no_op["pr_snapshot_sha256_after"] = after_no_op

        current_samples = [cast(float, first["pre_pr_preparation_seconds"])]
        baseline_median = (
            statistics.median(self.baseline_pre_pr_seconds)
            if self.baseline_pre_pr_seconds
            else None
        )
        current_median = statistics.median(current_samples)
        reduction = baseline_median / current_median if baseline_median is not None else None
        reduction_target = reduction >= 3.0 if reduction is not None else None
        self.evidence["field_publish"] = first
        self.evidence["no_op_republish"] = no_op
        self.evidence["pre_pr_preparation"] = {
            "baseline_samples_seconds": list(self.baseline_pre_pr_seconds),
            "baseline_median_seconds": baseline_median,
            "current_samples_seconds": current_samples,
            "current_median_seconds": current_median,
            "reduction_factor": reduction,
            "target_at_least_3x": reduction_target,
            "target_is_non_blocking": True,
        }
        progress = cast(dict[str, Any], first["progress"])
        self.evidence["live_targets"] = {
            "first_progress_within_one_second": progress["first_progress_within_one_second"],
            "pre_pr_preparation_at_least_3x_faster": reduction_target,
            "no_unexplained_silent_interval": progress["no_unexplained_silent_interval"],
            "targets_are_non_blocking": True,
        }
        self._write_evidence()


class Scenario:
    def __init__(self, source: Path, evidence_path: Path, repo_name: str | None) -> None:
        self.source = source.resolve()
        self.evidence_path = evidence_path.resolve()
        self.repo_name = repo_name
        self.owner = ""
        self.repo_slug = ""
        self.repo_url = ""
        self.work_root = Path(tempfile.mkdtemp(prefix="git-paoding-live-publish-"))
        self.repo = self.work_root / "repo"
        self.command_env = os.environ.copy()
        self.command_env["UV_CACHE_DIR"] = str(self.work_root / "uv-cache")
        self.evidence: dict[str, Any] = {
            "evidence_version": 1,
            "scenario": "five-step live publish workflow validation",
            "started_at": utc_now(),
            "source_commit": self.git_source("rev-parse", "HEAD").stdout.strip(),
            "steps": {},
        }
        self.isolation_checks: list[dict[str, Any]] = []

    def git_source(self, *args: str) -> CommandResult:
        return run(["git", *args], cwd=self.source)

    def git(self, *args: str, expected: int | tuple[int, ...] = 0) -> CommandResult:
        return run(["git", *args], cwd=self.repo, expected=expected)

    def gh(self, *args: str, expected: int | tuple[int, ...] = 0) -> CommandResult:
        return run(["gh", *args], cwd=self.repo, expected=expected)

    def api(self, endpoint: str, *args: str) -> Any:
        return parse_json(self.gh("api", endpoint, *args), context=f"gh api {endpoint}")

    def paoding(self, *args: str, expected: int | tuple[int, ...] = 0) -> CommandResult:
        return run(
            ["uv", "run", "--project", str(self.source), "git-paoding", *args],
            cwd=self.repo,
            expected=expected,
            env=self.command_env,
        )

    def paoding_json(self, *args: str, expected: int | tuple[int, ...] = 0) -> Any:
        result = self.paoding(*args, expected=expected)
        return parse_json(result, context=f"git-paoding {' '.join(args)}")

    def repo_snapshot(self) -> dict[str, str]:
        return {
            "head": self.git("rev-parse", "HEAD").stdout.strip(),
            "tree": self.git("rev-parse", "HEAD^{tree}").stdout.strip(),
            "status": self.git("status", "--porcelain=v1").stdout,
            "branch": self.git("symbolic-ref", "--short", "HEAD").stdout.strip(),
        }

    def publish_with_isolation(self) -> dict[str, Any]:
        before = self.repo_snapshot()
        payload = self.paoding_json("publish", "--json")
        after = self.repo_snapshot()
        require(before == after, "publish changed canonical HEAD/tree/branch or working tree")
        self.isolation_checks.append({"before": before, "after": after, "passed": True})
        return cast(dict[str, Any], payload)

    def remote_oid(self, short_ref: str) -> str:
        full_ref = f"refs/heads/{short_ref}"
        rows = self.git("ls-remote", "origin", full_ref).stdout.splitlines()
        require(len(rows) == 1, f"expected exactly one remote row for {full_ref}, got {rows}")
        oid, advertised_ref = rows[0].split("\t", maxsplit=1)
        require(advertised_ref == full_ref, f"unexpected advertised ref: {advertised_ref}")
        return oid

    def generated_oids(self, pr: dict[str, Any]) -> dict[str, str]:
        return {
            "base_ref": pr["base"]["ref"],
            "base_oid": self.remote_oid(pr["base"]["ref"]),
            "head_ref": pr["head"]["ref"],
            "head_oid": self.remote_oid(pr["head"]["ref"]),
        }

    def timeline(self, number: int) -> list[dict[str, Any]]:
        payload = self.api(
            f"repos/{self.repo_slug}/issues/{number}/timeline",
            "-H",
            "Accept: application/vnd.github+json",
        )
        require(isinstance(payload, list), "timeline response was not a list")
        return [
            {
                "id": item.get("id"),
                "event": item.get("event"),
                "created_at": item.get("created_at"),
                "commit_id": item.get("commit_id"),
            }
            for item in payload
        ]

    def pull(self, number: int) -> dict[str, Any]:
        payload = self.api(f"repos/{self.repo_slug}/pulls/{number}")
        require(isinstance(payload, dict), "pull response was not an object")
        return cast(dict[str, Any], payload)

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        payload = self.api(f"repos/{self.repo_slug}/pulls/{number}/files")
        require(isinstance(payload, list), "pull files response was not a list")
        return cast(list[dict[str, Any]], payload)

    def pr_snapshot(self, number: int) -> dict[str, Any]:
        pr = self.pull(number)
        timeline = self.timeline(number)
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "updated_at": pr["updated_at"],
            "body_sha256": sha256_text(pr.get("body") or ""),
            "head_oid": pr["head"]["sha"],
            "base_oid": pr["base"]["sha"],
            "timeline": timeline,
            "timeline_sha256": sha256_text(json.dumps(timeline, sort_keys=True)),
        }

    def marker_prs(self, marker: str) -> list[dict[str, Any]]:
        payload = self.api(f"repos/{self.repo_slug}/pulls?state=open&per_page=100")
        require(isinstance(payload, list), "open pull response was not a list")
        return [pr for pr in payload if marker in (pr.get("body") or "")]

    def wait_for_timeline_delta(
        self,
        number: int,
        before: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        before_ids = {event["id"] for event in before}
        for _attempt in range(15):
            after = self.timeline(number)
            delta = [event for event in after if event["id"] not in before_ids]
            if delta:
                return after, delta
            time.sleep(2)
        fail(f"GitHub recorded no timeline event after force-pushing PR #{number}")

    def normalized_diff(self, base_oid: str, head_oid: str) -> str:
        diff = self.git(
            "diff", "--no-renames", "--unified=3", base_oid, head_oid, "--", "scenario.txt"
        ).stdout
        return "\n".join(line for line in diff.splitlines() if not line.startswith("index "))

    def create_repo(self) -> None:
        for executable in ("git", "gh", "uv"):
            require(
                shutil.which(executable) is not None, f"missing required executable: {executable}"
            )
        self.repo.mkdir()
        self.gh("--version")
        self.gh("auth", "status")
        user = self.api("user")
        self.owner = user["login"]
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = self.repo_name or f"git-paoding-live-publish-{timestamp}"
        self.repo_slug = f"{self.owner}/{name}"
        self.repo_url = f"https://github.com/{self.repo_slug}"
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "git-paoding live publish validation")
        self.git("config", "user.email", "git-paoding@localhost")
        self.gh(
            "repo",
            "create",
            self.repo_slug,
            "--private",
            "--source",
            str(self.repo),
            "--remote",
            "origin",
            "--description",
            "Disposable-but-preserved git-paoding live publish validation evidence",
        )
        self.evidence.update(
            {
                "github_account": self.owner,
                "gh_version": self.gh("--version").stdout.splitlines()[0],
                "scratch_repo": self.repo_slug,
                "scratch_repo_url": self.repo_url,
                "scratch_repo_visibility": "private",
                "local_scratch_path": str(self.repo),
            }
        )

    def seed_canonical_history(self) -> None:
        scenario = self.repo / "scenario.txt"
        scenario.write_text(BASE_CONTENT, encoding="utf-8")
        self.git("add", "scenario.txt")
        self.git("commit", "--quiet", "-m", "Live publish validation base")
        self.git("push", "--quiet", "--set-upstream", "origin", "main")
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "--quiet", "-c", CANONICAL_BRANCH)
        scenario.write_text(BASE_CONTENT.replace('alpha = "base"', 'alpha = "slice-v1"'))
        self.git("add", "scenario.txt")
        self.git("commit", "--quiet", "-m", "Live publish validation Slice A v1")
        self.git("push", "--quiet", "--set-upstream", "origin", CANONICAL_BRANCH)
        self.evidence["base_oid"] = base_oid
        self.evidence["canonical_branch"] = CANONICAL_BRANCH

    def step1(self) -> tuple[int, int, dict[str, Any]]:
        pre_init = self.paoding("status", expected=1)
        require(
            "no git-paoding session" in pre_init.stderr.casefold(),
            "pre-init exit 1 was not a session error",
        )
        self.paoding("init", "--base", "origin/main")
        self.paoding("slice", "add", SLICE_A, "--title", "Primary review change")
        self.paoding("slice", "add", EMPTY_SLICE, "--title", "Empty slice contract")
        status = self.paoding_json("status", "--json", expected=2)
        require(status["contract_version"] == 0, "status contract version is not v0")
        require(status["unassigned_count"] == 1, "expected one unassigned Slice A atom")

        action_needed = self.paoding_json("publish", "--json", expected=2)
        require(action_needed["action_needed"] is True, "publish exit 2 lacked action_needed")
        require(
            not self.git("ls-remote", "--heads", "origin", "refs/heads/paoding/*").stdout,
            "action-needed publish wrote generated refs",
        )
        require(
            self.api(f"repos/{self.repo_slug}/pulls?state=open") == [],
            "action-needed publish created PRs",
        )

        self.paoding("assign", SLICE_A, "scenario.txt")
        published = self.publish_with_isolation()
        outcomes = {item["slice_id"]: item for item in published["slices"]}
        require(outcomes[SLICE_A]["outcome"] == "created", "Slice A was not created")
        require(outcomes[EMPTY_SLICE]["outcome"] == "empty", "empty slice was not reported")
        require(outcomes[EMPTY_SLICE]["pr_number"] is None, "empty slice unexpectedly got a PR")
        slice_number = outcomes[SLICE_A]["pr_number"]
        integration_number = published["integration_pr"]
        require(isinstance(slice_number, int), "missing Slice A PR number")
        require(isinstance(integration_number, int), "missing integration PR number")
        slice_pr = self.pull(slice_number)
        integration_pr = self.pull(integration_number)
        require(slice_pr["draft"] is True and integration_pr["draft"] is True, "PRs are not Draft")
        require("DO NOT MERGE" in slice_pr["body"], "slice banner is missing")
        require(
            f"<!-- paoding-slice-id: {SLICE_A} -->" in slice_pr["body"], "slice marker is missing"
        )
        require(
            "<!-- paoding-integration-pr -->" in integration_pr["body"],
            "integration marker is missing",
        )
        require(slice_pr["html_url"] in integration_pr["body"], "integration index lacks Slice A")

        refs = self.generated_oids(slice_pr)
        require(
            self.git("merge-base", refs["base_oid"], refs["head_oid"]).stdout.strip()
            == refs["base_oid"],
            "synthetic base is not the generated head merge-base",
        )
        expected = self.normalized_diff(self.evidence["base_oid"], self.repo_snapshot()["head"])
        actual = self.normalized_diff(refs["base_oid"], refs["head_oid"])
        require(actual == expected, "Slice A generated diff does not equal its owned Final diff")
        files = self.pull_files(slice_number)
        require(
            [item["filename"] for item in files] == ["scenario.txt"],
            "GitHub PR files are not exact",
        )

        self.evidence["steps"]["1_initial_publish"] = {
            "pre_init_exit": pre_init.returncode,
            "unassigned_status_exit": 2,
            "action_needed_publish_exit": 2,
            "successful_publish_exit": 0,
            "slice_pr_number": slice_number,
            "slice_pr_url": slice_pr["html_url"],
            "integration_pr_number": integration_number,
            "integration_pr_url": integration_pr["html_url"],
            "slice_pr_draft": slice_pr["draft"],
            "integration_pr_draft": integration_pr["draft"],
            "banner_present": True,
            "marker_present": True,
            "empty_slice_outcome": outcomes[EMPTY_SLICE],
            "generated_refs": refs,
            "merge_base": refs["base_oid"],
            "visible_patch_sha256": sha256_text(files[0].get("patch") or ""),
            "exact_slice_diff": True,
        }
        return slice_number, integration_number, slice_pr

    def step2(self, slice_number: int, integration_number: int, slice_pr: dict[str, Any]) -> None:
        time.sleep(2)
        refs_before = self.generated_oids(slice_pr)
        slice_before = self.pr_snapshot(slice_number)
        integration_before = self.pr_snapshot(integration_number)
        published = self.publish_with_isolation()
        outcome = next(item for item in published["slices"] if item["slice_id"] == SLICE_A)
        slice_after = self.pr_snapshot(slice_number)
        integration_after = self.pr_snapshot(integration_number)
        refs_after = self.generated_oids(self.pull(slice_number))
        require(outcome["outcome"] == "no-op", "unchanged publish was not reported as no-op")
        require(refs_after == refs_before, "unchanged publish changed generated refs/OIDs")
        require(slice_after == slice_before, "unchanged publish changed Slice A PR or timeline")
        require(integration_after == integration_before, "unchanged publish changed integration PR")
        self.evidence["steps"]["2_unchanged_no_op"] = {
            "publish_outcome": outcome["outcome"],
            "refs_before": refs_before,
            "refs_after": refs_after,
            "slice_pr_snapshot_before": slice_before,
            "slice_pr_snapshot_after": slice_after,
            "integration_snapshot_before": integration_before,
            "integration_snapshot_after": integration_after,
            "byte_identical_oids": True,
            "no_timeline_events": True,
            "no_body_edits": True,
        }

    def step3(self, slice_number: int) -> dict[str, Any]:
        old_pr = self.pull(slice_number)
        refs_before = self.generated_oids(old_pr)
        timeline_before = self.timeline(slice_number)
        scenario = self.repo / "scenario.txt"
        content = scenario.read_text(encoding="utf-8")
        scenario.write_text(content.replace('alpha = "slice-v1"', 'alpha = "slice-v2"'))
        self.git("add", "scenario.txt")
        self.git("commit", "--quiet", "-m", "Refresh Slice A to v2")
        self.git("push", "--quiet", "origin", CANONICAL_BRANCH)
        status = self.paoding_json("status", "--json")
        require(status["unassigned_count"] == 0, "exact-range Slice A refresh lost ownership")
        published = self.publish_with_isolation()
        outcome = next(item for item in published["slices"] if item["slice_id"] == SLICE_A)
        new_pr = self.pull(slice_number)
        refs_after = self.generated_oids(new_pr)
        require(outcome["pr_number"] == slice_number, "Slice A refresh changed PR number")
        require(outcome["outcome"] == "refreshed", "Slice A refresh was not reported")
        require(refs_before["base_oid"] != refs_after["base_oid"], "base ref was not rewritten")
        require(refs_before["head_oid"] != refs_after["head_oid"], "head ref was not rewritten")
        require(
            len(self.marker_prs(f"<!-- paoding-slice-id: {SLICE_A} -->")) == 1,
            "duplicate Slice A PR",
        )
        expected = self.normalized_diff(self.evidence["base_oid"], self.repo_snapshot()["head"])
        actual = self.normalized_diff(refs_after["base_oid"], refs_after["head_oid"])
        require(
            actual == expected and 'alpha = "slice-v2"' in actual, "Slice A refresh diff is wrong"
        )
        timeline_after, event_delta = self.wait_for_timeline_delta(slice_number, timeline_before)
        self.evidence["steps"]["3_same_pr_refresh"] = {
            "slice_pr_number_before": slice_number,
            "slice_pr_number_after": outcome["pr_number"],
            "slice_pr_url": new_pr["html_url"],
            "refs_before": refs_before,
            "refs_after": refs_after,
            "both_refs_force_pushed": True,
            "updated_diff_exact": True,
            "timeline_event_delta": event_delta,
            "timeline_after": timeline_after,
        }
        return new_pr

    def step4(self, slice_number: int, integration_number: int) -> dict[str, Any]:
        metadata = self.repo / ".git" / "paoding"
        require(metadata.is_dir(), "expected .git/paoding metadata before recovery")
        shutil.rmtree(metadata)
        missing = self.paoding("status", expected=1)
        require(
            "no git-paoding session" in missing.stderr.casefold(),
            "metadata loss did not degrade to no session",
        )
        self.paoding("init", "--base", "origin/main")
        self.paoding("slice", "add", SLICE_A, "--title", "Primary review change")
        self.paoding("slice", "add", EMPTY_SLICE, "--title", "Empty slice contract")
        status_before = self.paoding_json("status", "--json", expected=2)
        require(status_before["unassigned_count"] == 1, "re-init did not recover as unassigned")
        self.paoding("assign", SLICE_A, "scenario.txt")
        published = self.publish_with_isolation()
        outcome = next(item for item in published["slices"] if item["slice_id"] == SLICE_A)
        require(outcome["pr_number"] == slice_number, "marker recovery did not re-adopt Slice A PR")
        require(published["integration_pr"] == integration_number, "integration PR was duplicated")
        marker_matches = self.marker_prs(f"<!-- paoding-slice-id: {SLICE_A} -->")
        require(
            len(marker_matches) == 1 and marker_matches[0]["number"] == slice_number,
            "marker recovery duplicated PR",
        )
        status_after = self.paoding_json("status", "--json")
        slice_status = next(item for item in status_after["slices"] if item["id"] == SLICE_A)
        require(slice_status["pr_number"] == slice_number, "recovered mapping was not persisted")
        self.evidence["steps"]["4_marker_recovery"] = {
            "metadata_deleted": str(metadata),
            "missing_session_exit": missing.returncode,
            "reinitialized_unassigned_count": status_before["unassigned_count"],
            "original_slice_pr_number": slice_number,
            "recovered_slice_pr_number": outcome["pr_number"],
            "original_integration_pr_number": integration_number,
            "recovered_integration_pr_number": published["integration_pr"],
            "open_marker_pr_count": len(marker_matches),
            "persisted_pr_mapping": slice_status["pr_number"],
            "duplicate_pr_created": False,
        }
        return self.pull(slice_number)

    def step5(self, slice_number: int, slice_pr: dict[str, Any]) -> None:
        refs_before = self.generated_oids(slice_pr)
        files_before = self.pull_files(slice_number)
        patch_before = files_before[0].get("patch") or ""
        comment = self.api(
            f"repos/{self.repo_slug}/pulls/{slice_number}/comments",
            "--method",
            "POST",
            "-f",
            f"body={COMMENT_BODY}",
            "-f",
            f"commit_id={refs_before['head_oid']}",
            "-f",
            "path=scenario.txt",
            "-F",
            "line=2",
            "-f",
            "side=RIGHT",
        )
        comment_id = comment["id"]
        require(comment["line"] == 2, "inline comment was not anchored initially")
        timeline_before = self.timeline(slice_number)

        self.paoding("slice", "add", SLICE_B, "--title", "Unrelated same-file context")
        scenario = self.repo / "scenario.txt"
        content = scenario.read_text(encoding="utf-8")
        scenario.write_text(content.replace('beta = "base"', 'beta = "slice-b"'))
        self.git("add", "scenario.txt")
        self.git("commit", "--quiet", "-m", "Add Slice B in the same file")
        self.git("push", "--quiet", "origin", CANONICAL_BRANCH)
        status = self.paoding_json("status", "--json", expected=2)
        require(status["unassigned_count"] == 1, "Slice B edit was not surfaced as unassigned")
        assignment = self.paoding("assign", SLICE_B, "scenario.txt")
        require("-> context" in assignment.stdout, "Slice B atom was not assigned")
        published = self.publish_with_isolation()
        a_outcome = next(item for item in published["slices"] if item["slice_id"] == SLICE_A)
        b_outcome = next(item for item in published["slices"] if item["slice_id"] == SLICE_B)
        require(a_outcome["pr_number"] == slice_number, "unrelated refresh changed Slice A PR")
        require(a_outcome["outcome"] == "refreshed", "full-Final Slice A refs were not refreshed")
        require(b_outcome["outcome"] == "created", "Slice B PR was not created")

        refreshed_a = self.pull(slice_number)
        refs_after = self.generated_oids(refreshed_a)
        require(
            refs_before["base_oid"] != refs_after["base_oid"], "Slice A base ref did not refresh"
        )
        require(
            refs_before["head_oid"] != refs_after["head_oid"], "Slice A head ref did not refresh"
        )
        files_after = self.pull_files(slice_number)
        patch_after = files_after[0].get("patch") or ""
        require(patch_after == patch_before, "unrelated Slice B edit changed Slice A visible patch")
        require('beta = "slice-b"' not in patch_after, "Slice B leaked into Slice A visible patch")
        b_files = self.pull_files(b_outcome["pr_number"])
        require('beta = "slice-b"' in (b_files[0].get("patch") or ""), "Slice B patch is missing")
        require(
            'alpha = "slice-v2"' not in (b_files[0].get("patch") or ""),
            "Slice A leaked into Slice B patch",
        )
        timeline_after, event_delta = self.wait_for_timeline_delta(slice_number, timeline_before)

        survived: dict[str, Any] | None = None
        for _attempt in range(15):
            candidate = self.api(f"repos/{self.repo_slug}/pulls/comments/{comment_id}")
            if candidate.get("line") == 2 and candidate.get("path") == "scenario.txt":
                survived = candidate
                break
            time.sleep(2)
        require(survived is not None, "inline comment became outdated or disappeared after refresh")
        assert survived is not None
        self.evidence["steps"]["5_inline_comment_survival"] = {
            "slice_a_pr_number": slice_number,
            "slice_a_pr_url": refreshed_a["html_url"],
            "slice_b_pr_number": b_outcome["pr_number"],
            "slice_b_pr_url": b_outcome["url"],
            "inline_comment_id": comment_id,
            "inline_comment_url": survived["html_url"],
            "inline_comment_path": survived["path"],
            "inline_comment_line_before": comment["line"],
            "inline_comment_line_after": survived["line"],
            "inline_comment_original_commit_id": survived["original_commit_id"],
            "inline_comment_current_commit_id": survived["commit_id"],
            "refs_before": refs_before,
            "refs_after": refs_after,
            "both_slice_a_refs_force_pushed": True,
            "slice_a_visible_patch_sha256_before": sha256_text(patch_before),
            "slice_a_visible_patch_sha256_after": sha256_text(patch_after),
            "slice_a_visible_diff_unchanged": True,
            "slice_a_pr_identity_unchanged": True,
            "inline_comment_live_and_anchored": True,
            "same_file_two_slices": True,
            "timeline_event_delta": event_delta,
            "timeline_after": timeline_after,
        }

    def finish(self) -> None:
        self.evidence["canonical_final_oid"] = self.git("rev-parse", "HEAD").stdout.strip()
        self.evidence["canonical_final_tree_oid"] = self.git(
            "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        self.evidence["canonical_worktree_clean"] = (
            self.git("status", "--porcelain=v1").stdout == ""
        )
        self.evidence["canonical_isolation_checks"] = self.isolation_checks
        self.evidence["invariants"] = {
            "1_canonical_state": "one canonical live-publish-validation branch; all publish isolation snapshots passed",
            "2_no_stack_maintenance": "generated refs were never checked out or edited",
            "3_final_state": "each generated head OID tracked the current canonical Final",
            "4_diff_level": "Slice A and Slice B own separate hunks in scenario.txt",
            "5_single_owner": "live status/assign placed each atom under exactly one slice",
            "6_integration_correctness": "only the canonical integration branch is the integration PR head",
            "7_review_vs_approval": "slice PRs are Draft projections; integration PR remains authoritative",
            "8_stable_identity": "Slice A retained its PR number through both refresh and recovery",
            "9_regeneration": "deterministic generated refs were rebuilt solely from canonical state/metadata",
            "10_recoverability": "metadata deletion degraded to unassigned; marker recovered the PR mapping",
            "11_selective_update": "Slice B rewrote Slice A refs but not its patch, comment anchor, or PR identity",
            "12_github_history": "GitHub retained the live inline review comment on the unchanged Slice A hunk",
        }
        self.evidence["validated_behaviors"] = {
            "empty_slice_handling": "confirmed: a new empty slice was reported empty and no PR was created; existing-empty behavior also remains covered by the real-Git/fake-backend integration test",
            "integration_pull_request": "confirmed live: first successful publish auto-created one Draft integration PR and indexed slices",
            "exit_statuses": "confirmed live: operational/no-session=1, action-needed=2, success=0",
            "author_sign_off": "awaiting author sign-off",
        }
        self.evidence["contract_freeze"] = {
            "contract_version": 0,
            "scope": [
                "git_paoding.api facade signatures",
                "git_paoding.core.model types",
                "Session schema_version 1 store schema",
                "GitHubBackend Protocol",
                "status/assign-batch/publish JSON v0 schemas and payload goldens",
            ],
        }
        self.evidence["completed_at"] = utc_now()
        self.evidence["scratch_repo_retention"] = "preserved for author audit; no cleanup requested"
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(
            json.dumps(self.evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Evidence: {self.evidence_path}")
        print(f"Scratch repo preserved: {self.repo_url}")

    def execute(self) -> None:
        self.create_repo()
        self.seed_canonical_history()
        slice_number, integration_number, slice_pr = self.step1()
        self.step2(slice_number, integration_number, slice_pr)
        slice_pr = self.step3(slice_number)
        slice_pr = self.step4(slice_number, integration_number)
        self.step5(slice_number, slice_pr)
        self.finish()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="git-paoding source checkout used by uv (default: repository containing this script)",
    )
    result.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="path to write the complete JSON evidence record",
    )
    result.add_argument(
        "--repo-name",
        help="optional new repository name; default includes a UTC timestamp",
    )
    result.add_argument(
        "--atomic-push-probe",
        action="store_true",
        help="run only the self-cleaning atomic two-ref exact-lease capability probe",
    )
    result.add_argument(
        "--probe-repo",
        help="existing private live-validation scratch repository as OWNER/NAME",
    )
    result.add_argument(
        "--release-validation",
        action="store_true",
        help="run the field-shaped release validation against an existing scratch repository",
    )
    result.add_argument(
        "--release-repo",
        help="owner-authorized existing private scratch repository as OWNER/NAME",
    )
    result.add_argument(
        "--baseline-pre-pr-seconds",
        action="append",
        type=float,
        default=[],
        help="repeat for historical pre-PR preparation samples used by the non-blocking target",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    scenario: Scenario | AtomicPushProbe | ReleaseValidation | None = None
    if args.atomic_push_probe and args.release_validation:
        print(
            "LIVE VALIDATION FAILED: choose either --atomic-push-probe or --release-validation",
            file=sys.stderr,
        )
        return 1
    if args.atomic_push_probe:
        if not args.probe_repo:
            print(
                "LIVE VALIDATION FAILED: --atomic-push-probe requires --probe-repo", file=sys.stderr
            )
            return 1
        if args.repo_name:
            print(
                "LIVE VALIDATION FAILED: --repo-name cannot be used with --atomic-push-probe",
                file=sys.stderr,
            )
            return 1
        if args.release_repo or args.baseline_pre_pr_seconds:
            print(
                "LIVE VALIDATION FAILED: release options require --release-validation",
                file=sys.stderr,
            )
            return 1
    elif args.release_validation:
        if not args.release_repo:
            print(
                "LIVE VALIDATION FAILED: --release-validation requires --release-repo",
                file=sys.stderr,
            )
            return 1
        if args.probe_repo or args.repo_name:
            print(
                "LIVE VALIDATION FAILED: probe and repository-creation options cannot be used "
                "with --release-validation",
                file=sys.stderr,
            )
            return 1
    else:
        if args.probe_repo or args.release_repo or args.baseline_pre_pr_seconds:
            print(
                "LIVE VALIDATION FAILED: mode-specific options require their validation mode",
                file=sys.stderr,
            )
            return 1
    try:
        if args.atomic_push_probe:
            scenario = AtomicPushProbe(
                args.source,
                args.evidence,
                args.probe_repo,
            )
        elif args.release_validation:
            scenario = ReleaseValidation(
                args.source,
                args.evidence,
                args.release_repo,
                args.baseline_pre_pr_seconds,
            )
        else:
            scenario = Scenario(args.source, args.evidence, args.repo_name)
        scenario.execute()
    except Exception as error:
        if isinstance(scenario, Scenario) and scenario.repo_url:
            print(f"Scratch repo retained after failure: {scenario.repo_url}", file=sys.stderr)
        print(f"LIVE VALIDATION FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
