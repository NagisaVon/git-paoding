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
