# git-paoding

[![PyPI](https://img.shields.io/pypi/v/git-paoding)](https://pypi.org/project/git-paoding/)
[![Unit Tests](https://github.com/NagisaVon/git-paoding/actions/workflows/ci.yml/badge.svg)](https://github.com/NagisaVon/git-paoding/actions/workflows/ci.yml)

<!-- cspell:words paoding -->

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

## How to use

To install, give this repository URL to your coding agent and ask it to install
`git-paoding` for itself by following the
[agent installation guide](docs/agent-installation.md).

To slice, ask your coding agent to use `git-paoding` to turn the complete
committed change on your current branch into semantic review slices.

When the canonical branch already has an open integration pull request, initialize from it. This
is the recommended path because git-paoding validates the real PR base, local merge base, head,
and diffstat before pinning the session:

```bash
git-paoding init --pr <integration-pr-number-or-url>
git-paoding slice add <slice-id> --title "<review concern>"
git-paoding status --summary --json
git-paoding status --paths --action-needed-only --json
git-paoding status --path <exact-path> --json
git-paoding assign --batch <assignment-plan.json> --quiet --json
git-paoding status --summary --json
# Ask for authorization immediately before this remote mutation.
git-paoding publish --json --network-timeout 120
```

If no integration PR exists yet, pin an explicit branch that is available locally:

```bash
git-paoding init --base <integration-target-branch>
```

The angle-bracket values are placeholders, not defaults. `publish` creates or refreshes Draft
slice PRs and may create the authoritative integration PR; inspect its exit status before
retrying (`0` clean, `2` attribution needed, `1` operational error). See the packaged agent skill
for the compact status/batch workflow, recovery, progress, tracing, and timeout controls.
