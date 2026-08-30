# git-paoding

<!-- cspell:words paoding pipx PAODING -->

> **Draft:** This is pre-release documentation until the final skill dry-run and
> release review. Release-install commands remain provisional until the package
> is published.

**Agent writes globally. Humans review locally.**

`git-paoding` lets a coding agent keep one coherent implementation on one
canonical integration branch while presenting the final change as several small,
semantic Draft GitHub pull requests. Those **slice PRs** are review projections:
they help people understand one concern at a time, but they are not development
branches or merge targets.

The canonical integration PR remains authoritative. It contains the complete
change, runs CI, receives final approval, and merges. Review feedback goes back
into the canonical branch; a later `git-paoding publish` refreshes the
projections without creating a branch stack to restack.

The name comes from 庖丁解牛 (_Chef Ding carves the ox_): cutting along the
natural joints.

## Status

The package is preparing for its first release. The CLI supports the complete
review lifecycle:

- initialize a session with a pinned base;
- add, list, rename, and remove stable slice identities;
- inspect the current `Base -> Final` diff as atoms;
- assign atoms by ID, path, directory, glob, or Final line range;
- batch assignments and use an optional slice focus;
- publish or refresh Draft slice PRs and the Draft integration PR; and
- archive slice PRs and generated refs after the integration PR merges.

## Requirements and installation

`git-paoding` requires Python 3.11 or newer, Git, and
[GitHub CLI](https://cli.github.com/) 2.45.0 or newer. GitHub operations reuse
the account and credentials configured by `gh`; authenticate before initializing
a session:

```bash
gh auth login
gh auth status
```

To run the current source checkout:

```bash
git clone https://github.com/NagisaVon/git-paoding.git
cd git-paoding
uv sync --extra dev --locked
uv run git-paoding --help
```

The following release-install commands are a **release draft** until the package
is published. They must be re-run during the release review:

```bash
uv tool install git-paoding
pipx install git-paoding
python -m pip install git-paoding
```

Choose one installation method, not all three.

## Quickstart for authors

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
publishing. If it has not been pushed, obtain the author's approval before doing
so:

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
combine the interactive `--force` option with `--batch`.

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

- its generated base and head refs are disposable projections;
- it is not the branch where implementation work happens;
- its review is for comprehension, not authoritative approval; and
- only the integration PR represents the complete change and real merge target.

Use normal GitHub review features on a slice PR: read its narrative, inspect
Files changed, and leave inline comments. After feedback, the author updates the
canonical branch and refreshes the same slice PR. GitHub may mark comments
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
  committed. Do not delete it as a routine reset. If it is lost, recreate the
  session and the same stable slice IDs; attribution returns as unassigned,
  while existing open slice PRs can be adopted by their machine markers on the
  next clean publish.
- New PR bodies contain only their machine-managed region; the tool does not
  seed a narrative template. Human narrative added outside those delimiters is
  preserved byte-for-byte on refresh.

After GitHub reports the integration PR as merged, archive the generated review
surface without merging any slice PR:

```bash
git-paoding archive
```

## Documentation smoke test

Maintainers can exercise the documented CLI in an isolated local repository
with a bare Git remote and a fake GitHub CLI backend:

```bash
python3 docs/smoke_doc_commands.py
```

The release gate explicitly requires the complete integrated command surface:

```bash
PAODING_REQUIRE_FINAL_CLI=1 python3 docs/smoke_doc_commands.py
```

The smoke reuses the Python interpreter that launched it, so its child CLI has
the same installed dependencies. The strict run rejects a missing batch,
`--force`, focus, slice lifecycle, or archive command instead of silently
skipping it.
