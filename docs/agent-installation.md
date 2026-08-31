# Agent installation guide

<!-- cspell:words paoding pipx PAODING -->

Install both the deterministic Python CLI and one agent integration. Installing
only the Python package does not make the workflow discoverable to an agent;
installing both the standalone skill and marketplace plugin for the same agent
is unnecessary because they package the same `SKILL.md`.

Do not initialize a session, push, publish, or otherwise change a project while
performing installation unless the user separately asks for that work.

## Prerequisites

`git-paoding` requires Python 3.11 or newer, Git, and
[GitHub CLI](https://cli.github.com/) 2.45.0 or newer. GitHub operations reuse
the account and credentials configured by `gh`:

```bash
gh auth login
gh auth status
```

## Install the CLI

Choose one isolated-tool installer:

```bash
uv tool install git-paoding
pipx install git-paoding
```

If neither is available, install into the agent's intended Python environment:

```bash
python -m pip install git-paoding
```

If a PyPI release is not available, `uv` can install the current GitHub version:

```bash
uv tool install "git+https://github.com/NagisaVon/git-paoding.git"
```

For development from a source checkout instead:

```bash
git clone https://github.com/NagisaVon/git-paoding.git
cd git-paoding
uv sync --extra dev --locked
uv run git-paoding --help
```

Verify that both command forms resolve:

```bash
git-paoding --version
git paoding --version
```

## Install one agent integration

### Standalone skill

The installed Python distribution carries the skill used by both agent plugins.
Install it into the official personal skill directory for the current agent:

```bash
git-paoding agent install --target codex --scope user
git-paoding agent install --target claude --scope user
```

Run only the command for the current agent. To install both agents at once, use:

```bash
git-paoding agent install --target codex --target claude --scope user
```

Use `--scope project` for a project-local installation. Re-run with `--force`
after an upgrade only when replacing a locally modified installed skill is
intended. Codex uses `.agents/skills/git-paoding`; Claude Code uses
`.claude/skills/git-paoding`, under the home directory for user scope.

Verify the skill with `/skills`. Claude Code can also invoke `/git-paoding`
directly. Restart the agent only if the current session does not detect the new
top-level skill directory.

### Marketplace plugin

As an alternative to the standalone skill, install the skill-only plugin from
this repository's marketplace.

For Codex:

```bash
codex plugin marketplace add NagisaVon/git-paoding
codex plugin add git-paoding@git-paoding
```

The plugin then also appears in the Plugins Directory in the ChatGPT desktop
app, where its skill is invoked as `$git-paoding`.

For Claude Code:

```bash
claude plugin marketplace add NagisaVon/git-paoding
claude plugin install git-paoding@git-paoding --scope user
```

Run `/reload-plugins` if Claude Code requests it, then invoke the plugin skill as
`/git-paoding:git-paoding`.

After installation, let the installed skill drive review slicing; do not copy a
second operational runbook into the prompt.
