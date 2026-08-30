#!/usr/bin/env python3
# cspell:words paoding PAODING
"""Smoke the commands documented in README.md and docs/SKILL.md.

The default path exercises every command available on the current main branch in
an isolated repository, including a full publish against a local bare remote and
a stateful fake ``gh`` executable. Set ``PAODING_REQUIRE_FINAL_CLI=1`` after CLI
integration to require the batch, focus, slice lifecycle, and archive surfaces.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FINAL_COMMANDS = (
    "git-paoding assign --batch paoding-assignments.json",
    "git-paoding assign storage src/storage.py --force",
    "git-paoding focus storage",
    "git-paoding focus --clear",
    "git-paoding slice list",
    "git-paoding slice rename temporary --title 'Temporary renamed'",
    "git-paoding slice remove temporary",
    "git-paoding archive",
)


def run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run a command and fail with its complete output on a wrong exit."""

    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != expected:
        command = shlex.join(args)
        raise RuntimeError(
            f"{command} exited {completed.returncode}, expected {expected}\n{completed.stdout}"
        )
    return completed


def git(args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    """Run Git and return stripped output."""

    return run(["git", *args], cwd=cwd, env=env).stdout.strip()


def load_fake_state(path: Path) -> dict[str, Any]:
    """Load the fake GitHub PR database."""

    if not path.exists():
        return {"next_number": 1, "prs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_fake_state(path: Path, state: dict[str, Any]) -> None:
    """Persist the fake GitHub PR database."""

    path.write_text(json.dumps(state), encoding="utf-8")


def option_value(args: list[str], name: str) -> str:
    """Read one required option from fake-gh arguments."""

    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"fake gh call lacks {name}: {shlex.join(args)}") from error


def fake_gh(args: list[str]) -> int:
    """Implement the small gh surface used by the documentation smoke."""

    state_path_text = os.environ.get("PAODING_FAKE_GH_STATE")
    if not state_path_text:
        raise RuntimeError("PAODING_FAKE_GH_STATE is required")
    state_path = Path(state_path_text)

    if args == ["--version"]:
        print("gh version 2.45.0 (documentation smoke)")
        return 0
    if args == ["auth", "status"]:
        print("Logged in to example.invalid")
        return 0

    state = load_fake_state(state_path)
    prs: list[dict[str, Any]] = state["prs"]
    if args[:2] == ["pr", "list"]:
        print(json.dumps([pr for pr in prs if pr["state"] == "OPEN"]))
        return 0

    if args[:2] == ["pr", "create"]:
        number = int(state["next_number"])
        state["next_number"] = number + 1
        url = f"https://example.invalid/pull/{number}"
        record = {
            "number": number,
            "url": url,
            "title": option_value(args, "--title"),
            "body": option_value(args, "--body"),
            "state": "OPEN",
            "isDraft": "--draft" in args,
            "baseRefName": option_value(args, "--base"),
            "headRefName": option_value(args, "--head"),
        }
        prs.append(record)
        save_fake_state(state_path, state)
        print(url)
        return 0

    if args[:2] == ["pr", "view"]:
        selector = args[2]
        number = int(selector.rstrip("/").rsplit("/", 1)[-1].lstrip("#"))
        record = next(pr for pr in prs if pr["number"] == number)
        print(json.dumps(record))
        return 0

    if args[:2] == ["pr", "edit"]:
        number = int(args[2])
        record = next(pr for pr in prs if pr["number"] == number)
        record["title"] = option_value(args, "--title")
        record["body"] = option_value(args, "--body")
        save_fake_state(state_path, state)
        return 0

    if args[:2] == ["pr", "close"]:
        number = int(args[2])
        record = next(pr for pr in prs if pr["number"] == number)
        record["state"] = "CLOSED"
        save_fake_state(state_path, state)
        return 0

    raise RuntimeError(f"unsupported fake gh call: {shlex.join(args)}")


def paoding_command() -> list[str]:
    """Resolve a current-checkout git-paoding command without installing it."""

    override = os.environ.get("PAODING_COMMAND")
    if override:
        return shlex.split(override)

    executable = shutil.which("git-paoding")
    if executable:
        return [executable]

    python = shutil.which("python3") or sys.executable
    return [
        python,
        "-c",
        "from git_paoding.cli.main import main; main()",
        "--",
    ]


def install_fake_gh(bin_dir: Path) -> None:
    """Install a wrapper that dispatches back into this file."""

    wrapper = bin_dir / "gh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        '"--fake-gh" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def smoke_current_cli(command: list[str], workspace: Path, env: dict[str, str]) -> Path:
    """Exercise the current README and skill workflow end to end."""

    repository = workspace / "work"
    remote = workspace / "remote.git"
    repository.mkdir()
    git(["init", "--initial-branch=main"], cwd=repository, env=env)
    git(["config", "user.name", "Documentation Smoke"], cwd=repository, env=env)
    git(["config", "user.email", "docs-smoke@example.invalid"], cwd=repository, env=env)
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git(["add", "README.md"], cwd=repository, env=env)
    git(["commit", "-m", "base"], cwd=repository, env=env)
    git(["init", "--bare", str(remote)], cwd=workspace, env=env)
    git(["remote", "add", "origin", str(remote)], cwd=repository, env=env)
    git(["push", "-u", "origin", "main"], cwd=repository, env=env)
    git(["switch", "-c", "feature/review"], cwd=repository, env=env)
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "storage.py").write_text("VALUE = 'stored'\n", encoding="utf-8")
    (repository / "tests" / "test_storage.py").write_text(
        "def test_storage():\n    assert True\n", encoding="utf-8"
    )
    git(["add", "src/storage.py", "tests/test_storage.py"], cwd=repository, env=env)
    git(["commit", "-m", "add storage behavior"], cwd=repository, env=env)
    git(["push", "-u", "origin", "HEAD"], cwd=repository, env=env)

    run([*command, "--help"], cwd=repository, env=env)
    run([*command, "init", "--base", "origin/main"], cwd=repository, env=env)
    run(
        [*command, "slice", "add", "storage", "--title", "Storage boundary"],
        cwd=repository,
        env=env,
    )
    run(
        [*command, "slice", "add", "tests", "--title", "Storage behavior tests"],
        cwd=repository,
        env=env,
    )
    status = run([*command, "status", "--json"], cwd=repository, env=env, expected=2)
    payload = json.loads(status.stdout)
    if payload["unassigned_count"] != 2:
        raise RuntimeError(f"expected two unassigned atoms, got {payload!r}")

    run([*command, "assign", "storage", "src/storage.py"], cwd=repository, env=env)
    run([*command, "assign", "tests", "tests/test_storage.py"], cwd=repository, env=env)
    run([*command, "status", "--json"], cwd=repository, env=env)
    first = run([*command, "publish", "--json"], cwd=repository, env=env)
    first_payload = json.loads(first.stdout)
    if first_payload["action_needed"] or len(first_payload["slices"]) != 2:
        raise RuntimeError(f"unexpected first publish result: {first_payload!r}")
    second = run([*command, "publish", "--json"], cwd=repository, env=env)
    second_payload = json.loads(second.stdout)
    if {item["outcome"] for item in second_payload["slices"]} != {"no-op"}:
        raise RuntimeError(f"unchanged publish was not a no-op: {second_payload!r}")
    return repository


def final_cli_available(command: list[str], workspace: Path, env: dict[str, str]) -> bool:
    """Check whether all draft command groups and options have landed."""

    top = run([*command, "--help"], cwd=workspace, env=env).stdout
    assign = run([*command, "assign", "--help"], cwd=workspace, env=env).stdout
    slice_help = run([*command, "slice", "--help"], cwd=workspace, env=env).stdout
    return (
        all(name in top for name in ("focus", "archive"))
        and all(option in assign for option in ("--batch", "--force"))
        and all(name in slice_help for name in ("list", "rename", "remove"))
    )


def smoke_final_cli(command: list[str], repository: Path, env: dict[str, str]) -> None:
    """Exercise draft commands once all planned surfaces are available."""

    batch_path = repository / "paoding-assignments.json"
    batch_path.write_text(
        json.dumps(
            {
                "contract_version": 0,
                "assignments": {
                    "storage": ["src/storage.py"],
                    "tests": ["tests/test_storage.py"],
                },
                "force": False,
            }
        ),
        encoding="utf-8",
    )
    run(
        [*command, "assign", "--batch", "paoding-assignments.json"],
        cwd=repository,
        env=env,
    )
    run(
        [*command, "assign", "storage", "src/storage.py", "--force"],
        cwd=repository,
        env=env,
    )
    run([*command, "focus", "storage"], cwd=repository, env=env)
    run([*command, "status", "--json"], cwd=repository, env=env)
    run([*command, "focus", "--clear"], cwd=repository, env=env)
    run(
        [*command, "slice", "add", "temporary", "--title", "Temporary"],
        cwd=repository,
        env=env,
    )
    run(
        [
            *command,
            "slice",
            "rename",
            "temporary",
            "--title",
            "Temporary renamed",
        ],
        cwd=repository,
        env=env,
    )
    run([*command, "slice", "list"], cwd=repository, env=env)
    run([*command, "slice", "remove", "temporary"], cwd=repository, env=env)
    run([*command, "archive"], cwd=repository, env=env)


def main() -> int:
    """Run the current smoke and enforce final commands when requested."""

    if len(sys.argv) >= 2 and sys.argv[1] == "--fake-gh":
        return fake_gh(sys.argv[2:])

    command = paoding_command()
    require_final = os.environ.get("PAODING_REQUIRE_FINAL_CLI") == "1"
    with tempfile.TemporaryDirectory(prefix="git-paoding-doc-smoke-") as temp:
        workspace = Path(temp)
        bin_dir = workspace / "bin"
        bin_dir.mkdir()
        install_fake_gh(bin_dir)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["PAODING_FAKE_GH_STATE"] = str(workspace / "fake-gh-state.json")
        env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
        repository = smoke_current_cli(command, workspace, env)

        available = final_cli_available(command, workspace, env)
        if require_final and not available:
            pending = "\n".join(f"  - {item}" for item in FINAL_COMMANDS)
            raise RuntimeError(f"final CLI integration is incomplete; verify:\n{pending}")
        if available:
            smoke_final_cli(command, repository, env)
            print("Current and final documented commands passed.")
        else:
            print("Current documented commands passed.")
            print("Draft commands pending CLI integration and strict smoke:")
            for item in FINAL_COMMANDS:
                print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
