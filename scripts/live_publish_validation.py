#!/usr/bin/env python3
"""Run the five-step publish validation against a newly created GitHub scratch repo.

This is a manual, networked workflow. It is intentionally excluded from pytest/CI.
The created private repository and its Draft PRs are preserved for human audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

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


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


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


def parse_json(result: CommandResult, *, context: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"{context} returned invalid JSON: {error}\n{result.stdout}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
        return payload

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
        return payload

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        payload = self.api(f"repos/{self.repo_slug}/pulls/{number}/files")
        require(isinstance(payload, list), "pull files response was not a list")
        return payload

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
    return result


def main() -> int:
    args = parser().parse_args()
    scenario = Scenario(args.source, args.evidence, args.repo_name)
    try:
        scenario.execute()
    except Exception as error:
        if scenario.repo_url:
            print(f"Scratch repo retained after failure: {scenario.repo_url}", file=sys.stderr)
        print(f"LIVE VALIDATION FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
