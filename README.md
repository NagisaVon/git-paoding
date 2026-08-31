# git-paoding

<!-- cspell:words paoding pipx PAODING -->

Agent writes globally. Humans review locally.

`git-paoding` lets a coding agent keep one coherent implementation on one
canonical integration branch while presenting the final change as several small,
semantic Draft GitHub pull requests. Those slice PRs are review projections:
they help people understand one concern at a time, but they are not development
branches or merge targets.

The canonical integration PR remains authoritative. It contains the complete
change, runs CI, receives final approval, and merges. Review feedback goes back
into the canonical branch; a later `git-paoding publish` refreshes the
projections without creating a branch stack to restack.

The name comes from 庖丁解牛 (_Chef Ding carves the ox_): cutting along the
natural joints.

## Status

The CLI supports the complete review lifecycle:

- Initialize a session with a pinned base.
- Add, list, rename, and remove stable slice identities.
- Inspect the current `Base -> Final` diff as atoms.
- Assign atoms by ID, path, directory, glob, or Final line range.
- Batch assignments and use an optional slice focus.
- Publish or refresh Draft slice PRs and the Draft integration PR.
- Archive slice PRs and generated refs after the integration PR merges.

## Install the CLI and agent workflow

`git-paoding` is designed for coding agents rather than as a human-operated UI.
A complete installation has two parts:

1. The Python package provides the deterministic `git-paoding` executable.
2. The agent skill or plugin teaches Codex or Claude Code when and how to use it.

Installing only the Python package does not make an agent discover the workflow.
Install the CLI and one agent integration together.

### Give this page to an agent

You can send an agent this repository URL and the following request:

```text
Install git-paoding by following the repository README. Install the Python CLI
and the integration for the agent you are running as, verify both, and then
explain the workflow. Do not initialize a session, push, publish, or change any
repository until I give you a specific review-slicing task.
```

### 1. Install the CLI

`git-paoding` requires Python 3.11 or newer, Git, and
[GitHub CLI](https://cli.github.com/) 2.45.0 or newer. GitHub operations reuse
the account and credentials configured by `gh`; authenticate before initializing
a session:

```bash
gh auth login
gh auth status
```

Install the released package with one of these methods. `uv tool` or `pipx` is
recommended because it keeps the agent-facing command isolated:

```bash
uv tool install git-paoding
pipx install git-paoding
python -m pip install git-paoding
```

Choose one installation method, not all three. Confirm the explicit command and
Git's external-subcommand form resolve to the same release:

```bash
git-paoding --version
git paoding --version
```

If the first PyPI release is not available yet, install the current GitHub
version directly:

```bash
uv tool install "git+https://github.com/NagisaVon/git-paoding.git"
```

To install from a source checkout instead:

```bash
git clone https://github.com/NagisaVon/git-paoding.git
cd git-paoding
uv sync --extra dev --locked
uv run git-paoding --help
```

### 2. Install the agent integration

Choose one of the following installation methods for each agent. The bundled
standalone skill and the marketplace plugin provide the same instructions, so
installing both for the same agent is unnecessary.

#### Option A: install the bundled standalone skill

The Python distribution carries the same `SKILL.md` used by both plugins. The
following commands copy that bundled skill into the official personal skill
directory and work without a plugin UI:

For Codex:

```bash
git-paoding agent install --target codex --scope user
```

For Claude Code:

```bash
git-paoding agent install --target claude --scope user
```

To install both, repeat `--target` in one command:

```bash
git-paoding agent install --target codex --target claude --scope user
```

Use `--scope project` to install into the current repository instead of the
current user's global skill directory. Re-run with `--force` after upgrading if
the installed skill was modified locally. Codex installs to
`.agents/skills/git-paoding`; Claude Code installs to
`.claude/skills/git-paoding` (under the home directory for user scope).

Verify the skill appears in Codex with `/skills`. In Claude Code, run `/skills`
or invoke `/git-paoding` directly. Restart the agent only if a newly created
top-level skill directory is not detected in the current session.

#### Option B: install through the plugin marketplace

This repository is also a marketplace for a skill-only `git-paoding` plugin.
The Codex and Claude Code manifests share one packaged skill, so their behavior
does not drift.

For Codex, add the GitHub marketplace:

```bash
codex plugin marketplace add NagisaVon/git-paoding
codex plugin add git-paoding@git-paoding
```

The same plugin then appears in the Plugins Directory in the ChatGPT desktop
app. Invoke its skill as `$git-paoding`.

For Claude Code, the complete installation is available from the CLI:

```bash
claude plugin marketplace add NagisaVon/git-paoding
claude plugin install git-paoding@git-paoding --scope user
```

Run `/reload-plugins` if Claude Code asks for it, then invoke the plugin skill as
`/git-paoding:git-paoding`.

### 3. Ask the agent to prepare review slices

For Codex:

```text
$git-paoding Prepare semantic review slices for the complete committed change
on my current branch, using origin/main as the base. Inspect and propose the
slice assignments first. Do not push or publish until I approve the plan.
```

For the Claude Code plugin:

```text
/git-paoding:git-paoding Prepare semantic review slices for the complete
committed change on my current branch, using origin/main as the base. Inspect
and propose the slice assignments first. Do not push or publish until I approve
the plan.
```

## Run a review-slicing session

After installation, Codex and Claude Code follow the same operational workflow
below. Their invocation syntax differs (`$git-paoding` in Codex and
`/git-paoding:git-paoding` for the Claude Code plugin), but both use the same
`git-paoding` CLI and repository state.

Start from the branch that contains the complete, committed implementation. The
base is pinned when the session is initialized; moving `origin/main` later does
not silently move that pin.

```bash
git-paoding init --base origin/main --slice-prefix ABC-123
git-paoding slice add storage --title "Storage boundary"
git-paoding slice add tests --title "Storage behavior tests"
git-paoding status --json
```

`status` is local and read-only. Exit code `2` is expected while it reports
unassigned or ambiguous atoms. Its JSON includes each atom's ID, path, Base and
Final ranges, owner, state, and short preview. Use `git-paoding status --full`
when complete changed-hunk previews are useful.

`--slice-prefix` is optional and defaults to `slice`. It changes only generated
slice PR titles, such as `[ABC-123] Storage boundary`; slice IDs and generated
refs remain stable. The integration PR title is the canonical branch name.

Assign interactively by an atom ID, path, directory, glob, or Final-coordinate
line range. Broad selectors preserve already-owned atoms unless `--force` is
passed; explicit atom IDs may reassign their exact atom without it. Every
selected atom is echoed as assigned or skipped:

```bash
git-paoding assign storage src/storage.py
git-paoding assign tests tests/test_storage.py
git-paoding status --json
```

When no action is needed, publish the review projections:

```bash
git-paoding publish
```

`publish` is idempotent. It reconciles first and stops with exit code `2` and no
remote effects if attribution still needs attention. A clean publish pushes
generated projection refs, creates or refreshes stable Draft slice PRs, and
creates or updates the Draft integration PR and its slice index. Operational
failures use exit code `1`; success uses `0`.

Keep the canonical branch available on the selected Git remote before
publishing. If it has not been pushed, obtain the change owner's approval before
doing so:

```bash
git push -u origin HEAD
```

### Three-step agent flow

The intended agent loop is:

```bash
git-paoding status --json
git-paoding assign --batch paoding-assignments.json
git-paoding publish
```

The batch request uses the frozen versioned contract:

```json
{
  "contract_version": 0,
  "assignments": {
    "storage": ["src/storage.py"],
    "tests": ["tests/test_storage.py"]
  },
  "force": false
}
```

Batch assignment is all-or-nothing: an unknown slice, invalid selector, or
cross-slice conflict rejects the entire request. Set the JSON `force` field to
`true` when a batch is intentionally repartitioning already-owned atoms; do not
combine the interactive `--force` option with `--batch`. The batch plan is an
ordinary local input file rather than session metadata; keep it untracked or
manage it according to the repository's own policy.

For targeted review feedback, focus may provide a default owner for genuinely
new atoms without overwriting confidently matched ownership:

```bash
git-paoding focus storage
git-paoding status --json
git-paoding focus --clear
```

## What reviewers should know

A slice PR is a view onto one semantic part of the final integrated change.
Different slices may touch different regions of the same file, and one slice may
rely on code shown by another. A slice is not required to build or test by
itself.

Every slice PR is Draft and carries a
**DO NOT MERGE — review projection only** warning because:

- Its generated base and head refs are disposable projections.
- It is not the branch where implementation work happens.
- Its review is for comprehension, not authoritative approval.
- Only the integration PR represents the complete change and real merge target.

Use normal GitHub review features on a slice PR: read its narrative, inspect
Files changed, and leave inline comments. After feedback, update the canonical
branch and refresh the same slice PR. GitHub may mark comments
outdated when their lines change; the discussion history and stable PR identity
remain useful.

## Suppress CI for slice PRs

Slice projections are review units, not integration units. In consumer
repositories, filter the `pull_request` workflow to real target branches so
generated `paoding/.../base` refs do not start authoritative CI. For a
repository that merges into `main`:

```yaml
name: CI

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
```

GitHub evaluates the `pull_request.branches` filter against the PR's base
branch. Keep the normal CI and branch-protection requirements on the integration
PR.

## Safety and recovery

- Work only on the canonical integration branch. Never check out or edit
  generated `paoding/...` branches.
- Never merge a slice PR. Close/archive it after the integration PR merges.
- Do not expect a slice projection to build, test, or pass CI independently.
- Unassigned or ambiguous atoms are normal recovery states, not metadata
  corruption. Rerun `status`, classify what remains, and publish again.
- Slice metadata lives in the repository's common Git directory and is not
  committed. Do not delete it as a routine reset.
- If session metadata is lost, recreate the session and the same stable slice
  IDs. Attribution returns as unassigned, while existing open slice PRs can be
  adopted by their machine markers on the next clean publish.
- New PR bodies contain only their machine-managed region; the tool does not
  seed a narrative template. Human narrative added outside those delimiters is
  preserved byte-for-byte on refresh.

After GitHub reports the integration PR as merged, archive the generated review
surface without merging any slice PR:

```bash
git-paoding archive
```
