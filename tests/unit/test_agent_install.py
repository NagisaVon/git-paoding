"""Tests for the packaged Codex and Claude skill installers."""

from __future__ import annotations

import json
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
