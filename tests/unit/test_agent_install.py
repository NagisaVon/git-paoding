"""Tests for the packaged Codex and Claude skill installers."""

from __future__ import annotations

import json
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
from click.testing import CliRunner

from git_paoding import __version__
from git_paoding.agent_install import AgentInstallError, install_agent_skill
from git_paoding.cli.main import main


def _packaged_skill_text() -> str:
    return (
        files("git_paoding._agent_plugins")
        .joinpath("git-paoding")
        .joinpath("skills")
        .joinpath("git-paoding")
        .joinpath("SKILL.md")
        .read_text(encoding="utf-8")
    )


def _documented_commands() -> list[str]:
    return [
        line.strip()
        for line in _packaged_skill_text().splitlines()
        if line.startswith("git-paoding ")
    ]


@pytest.mark.unit
def test_installs_codex_and_claude_skills_at_project_scope(tmp_path: Path) -> None:
    codex = install_agent_skill("codex", "project", project_root=tmp_path)
    claude = install_agent_skill("claude", "project", project_root=tmp_path)

    assert codex.changed is True
    assert claude.changed is True
    assert codex.destination == tmp_path / ".agents" / "skills" / "git-paoding"
    assert claude.destination == tmp_path / ".claude" / "skills" / "git-paoding"
    assert (codex.destination / "SKILL.md").read_text(encoding="utf-8") == _packaged_skill_text()
    assert (claude.destination / "SKILL.md").read_text(encoding="utf-8") == _packaged_skill_text()


@pytest.mark.unit
def test_user_scope_is_idempotent_and_requires_force_for_different_content(
    tmp_path: Path,
) -> None:
    first = install_agent_skill("codex", "user", home=tmp_path)
    second = install_agent_skill("codex", "user", home=tmp_path)
    skill_file = first.destination / "SKILL.md"
    skill_file.write_text("locally modified\n", encoding="utf-8")

    assert first.changed is True
    assert second.changed is False
    with pytest.raises(AgentInstallError, match="--force"):
        install_agent_skill("codex", "user", home=tmp_path)

    forced = install_agent_skill("codex", "user", home=tmp_path, force=True)

    assert forced.changed is True
    assert skill_file.read_text(encoding="utf-8") == _packaged_skill_text()


@pytest.mark.unit
def test_agent_install_cli_can_install_both_project_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        main,
        [
            "agent",
            "install",
            "--target",
            "codex",
            "--target",
            "claude",
            "--scope",
            "project",
        ],
    )

    assert result.exit_code == 0
    assert (tmp_path / ".agents/skills/git-paoding/SKILL.md").is_file()
    assert (tmp_path / ".claude/skills/git-paoding/SKILL.md").is_file()
    assert "Installed codex skill" in result.output
    assert "Installed claude skill" in result.output


@pytest.mark.unit
def test_plugin_manifests_and_marketplaces_match_package_version() -> None:
    root = Path(__file__).resolve().parents[2]
    plugin = root / "src" / "git_paoding" / "_agent_plugins" / "git-paoding"
    codex_manifest = json.loads(
        (plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    claude_manifest = json.loads(
        (plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        (root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    claude_marketplace = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert codex_manifest["version"] == claude_manifest["version"] == __version__
    assert codex_manifest["skills"] == "./skills/"
    assert codex_marketplace["plugins"][0]["source"]["path"] == (
        "./src/git_paoding/_agent_plugins/git-paoding"
    )
    assert claude_marketplace["plugins"][0]["source"] == (
        "./src/git_paoding/_agent_plugins/git-paoding"
    )
    assert (plugin / "skills" / "git-paoding" / "SKILL.md").is_file()


@pytest.mark.unit
def test_project_and_lock_versions_match_package_version() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package
        for package in lock["package"]
        if package["name"] == "git-paoding" and package["source"] == {"editable": "."}
    )

    assert project["project"]["version"] == locked_project["version"] == __version__


@pytest.mark.unit
def test_packaged_skill_has_one_pr_first_bounded_workflow() -> None:
    text = _packaged_skill_text()
    commands = _documented_commands()
    status_commands = [command for command in commands if command.startswith("git-paoding status")]

    assert commands.index(
        "git-paoding init --pr <integration-pr-number-or-url> --slice-prefix ABC-123"
    ) < commands.index("git-paoding init --base <integration-target-branch> --slice-prefix ABC-123")
    summary = "git-paoding status --summary --json"
    paths = "git-paoding status --paths --action-needed-only --json"
    targeted = "git-paoding status --path <exact-path> --path <related-path> --json"
    assert {summary, paths, targeted}.issubset(status_commands)
    assert (
        status_commands.index(summary)
        < status_commands.index(paths)
        < status_commands.index(targeted)
    )
    assert status_commands.count(summary) >= 2
    assert "git-paoding status --json" not in commands
    assert "### 1. Inspect" not in text
    assert commands.count("git-paoding publish --json --trace --network-timeout 120") == 1


@pytest.mark.unit
def test_packaged_skill_covers_decision_changing_v012_interfaces() -> None:
    text = _packaged_skill_text()
    normalized = " ".join(text.split())
    commands = _documented_commands()

    assert {
        "git-paoding init --replace --pr <correct-integration-pr-number-or-url>",
        "git-paoding init --replace --base <correct-integration-target-branch>",
        "git-paoding assign --batch paoding-assignments.json --quiet --json",
        "git-paoding archive",
    }.issubset(commands)
    for decision in (
        "`init --base` is local-only",
        "each `preview` value as an empty string",
        "Progress and trace output go to stderr",
        "atomic push with an exact lease per destination",
        "concurrent publisher",
        "compatibility fallback is deliberately disabled",
        "`replace_session()`",
        "`StatusViewResult` v0",
    ):
        assert decision in normalized

    assert all(
        decision in normalized
        for decision in (
            "sandboxed authentication or connection failure",
            "identical read-only command outside the sandbox",
            "elevated retry returns HTTP 401",
            "Never expose tokens or start `gh auth login`",
        )
    )
    assert all(
        decision in normalized
        for decision in (
            "`--summary` and `--paths` are mutually exclusive",
            "Repeatable `--path` is an atom view and cannot combine with either one",
            "`--full` expands atom previews only",
        )
    )
