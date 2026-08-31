---
name: git-paoding
description:
  Prepare and publish semantic review slices for a large committed change while
  keeping one canonical integration branch. Use when an author wants focused
  Draft GitHub PRs for comprehension without creating a development branch
  stack.
---

<!-- cspell:words paoding buildability PAODING -->

# git-paoding

Use `git-paoding` to partition the current `Base -> Final` diff into human-sized
semantic review slices. Keep one canonical integrated implementation state
throughout.

## Preserve the mental model

- Edit, test, lint, and commit only on the canonical integration branch.
- Treat a slice as a review unit, never as a development phase, historical
  commit range, buildable unit, or merge target.
- Never check out, edit, rebase, or restack generated `paoding/...` branches.
  They are disposable projections derived from the canonical state and local
  slice metadata.
- Let `publish` manage generated refs and stable PR identities. Do not manually
  force-push projections or close and recreate a slice PR merely to refresh it.
- Never merge a slice PR. It is a Draft PR marked **DO NOT MERGE — review
  projection only**.
- Keep authoritative CI, approval, branch protection, and merge on the
  integration PR.
- Do not require slice-level CI or independent buildability. Cross-slice
  dependencies are valid.
- New PR bodies contain no narrative template. Preserve human-written PR
  narrative outside machine-managed regions byte-for-byte.

The operating rule is: **run `git-paoding publish` and do what it says**.
Publication is idempotent and self-checking; there is no separate refresh
ritual.

## Plan for review without bookkeeping every edit

When review concerns are already apparent, use them as provisional,
low-frequency structure guidance:

- Prefer one primary review concern per new module or test file when that is a
  natural boundary.
- Separate meaningfully different test concerns when ordinary readable
  organization permits.
- Avoid one large contiguous insertion that mixes unrelated concerns when a
  natural separation already exists.

This is optional guidance, not a correctness precondition. Do not create
artificial files, blank lines, awkward abstractions, or inferior architecture
merely to manufacture atom boundaries. Do not require active slices or
attribution calls during the normal edit-test-lint-fix loop. Provisional
concerns may change.

If nobody planned slice boundaries early, continue normally. Attribution is lazy
and recoverable: `status` will report unassigned or ambiguous atoms at the
review checkpoint, and those atoms can be classified then. Forgotten bookkeeping
never makes publication irrecoverable.

## Prepare the session

Work from the branch containing the complete committed change. Confirm the CLI
and `gh` 2.45.0 or newer are available, and the canonical branch exists locally.
Do not push a branch without the change owner's authorization.

When an open integration PR already exists, initialize from it. This is the
recommended path because the command validates the PR's real base/head identity,
local merge base, and diffstat without fetching:

```bash
git-paoding --version
gh --version
git-paoding init --pr <integration-pr-number-or-url> --slice-prefix ABC-123
git-paoding slice add storage --title "Storage boundary"
git-paoding slice add tests --title "Storage behavior tests"
```

If the canonical branch does not yet have an integration PR, initialize from an
explicit branch that already exists locally:

```bash
git-paoding init --base <real-integration-target-branch> --slice-prefix ABC-123
```

The angle-bracket values are placeholders, never defaults. `--pr` accepts a PR
number or URL. The local PR head branch and both PR endpoint commits must already
be available; if they are stale or missing, follow the printed fetch/checkout
instructions, then retry. git-paoding never fetches automatically.

Omit `--slice-prefix` to use `slice`. The prefix changes only slice PR titles;
it does not change stable slice IDs or generated refs. An integration PR that
`git-paoding` creates uses the canonical branch name as its title. If publish
adopts an existing integration PR, it preserves that PR's title exactly.

The base is pinned to a commit at initialization. Choose the real integration
target; do not silently change it later.

A read-only `gh` failure inside a sandbox is inconclusive; retry the identical
command out-of-sandbox via the platform's approval mechanism; diagnose invalid
credentials only on a confirmed HTTP 401; never print token contents; never
start `gh auth login` before that confirmation.

## Classify and publish

For a large diff, discover and assign the work in one bounded pass:

1. Read global size and attribution counts with
   `git-paoding status --summary --json`.
2. Find only paths that need decisions with
   `git-paoding status --paths --action-needed-only --json`.
3. Inspect atoms for each relevant path with
   `git-paoding status --path <exact-path> --json`; repeat `--path` when a
   related group should be considered together, and add `--full` only when the
   complete changed-hunk previews are necessary.
4. Prepare one complete batch plan and apply it once with
   `git-paoding assign --batch paoding-assignments.json --quiet --json`. The
   quiet JSON keeps every assignment record and identity field while leaving
   each `preview` string empty.
5. Confirm the global result with `git-paoding status --summary --json` before
   publishing.

Filtered status commands still use global unassigned and ambiguous counts for
their exit code. An empty filtered result can therefore exit `2` when work
remains elsewhere in the session.

### 1. Inspect

```bash
git-paoding status --json
```

Interpret exit code `2` as action needed, not an operational failure. Read
the versioned JSON atom list. Use atom IDs for precise selection; paths,
directories, globs, and Final-coordinate ranges select broader atom sets.

Line ranges use Final coordinates: `path:20-45` means lines 20 through 45 in the
file at the canonical branch tip. A partial range selects the whole atom; ranges
do not split atoms.

Previews are short. Run `git-paoding status --full` for complete changed-hunk
previews. If surrounding context is still needed, read the current file at
`final_start` through `final_start + final_len - 1` before assigning; do not
guess from the preview. For a deletion or other atom with no Final lines,
inspect the relevant Base-side change instead.

### 2. Assign

Prepare `paoding-assignments.json` using the frozen batch contract:

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

Then run:

```bash
git-paoding assign --batch paoding-assignments.json
```

The batch plan is ordinary local input, not git-paoding session metadata. Keep
it untracked, place it outside the working tree, or manage it according to the
repository's own policy; do not commit it accidentally.

For an interactive assignment, pass the slice followed by one or more
selectors:

```bash
git-paoding assign storage src/storage.py
git-paoding assign tests tests/test_storage.py
```

Review the assignment echo: it must list every moved or skipped atom with a
preview. Path and range selectors should claim only unassigned atoms by default.
Reassignment through a broad selector requires the explicit `--force` option;
an exact atom ID may take its atom directly. For batch repartitioning, set the
JSON `force` field instead of combining `--force` with `--batch`. If a selector
matches nothing or an atom ID became stale after a new commit, rerun
`status --json` rather than guessing.

### 3. Publish

```bash
git-paoding publish --network-timeout 120
```

Progress is written to stderr before each named phase and repeated before a
network process, while the final result remains on stdout. Use `--quiet` when a
machine consumer needs only the final result, `--trace` to append aggregate
phase/process timings without command arguments, and
`--network-timeout <seconds>` to bound each Git or GitHub network subprocess. A value of `0`
disables the timeout. These options compose with `--json`.

Respond to the result by exit code:

- `0`: clean success; report the integration PR and slice PRs.
- `2`: attribution needs attention; no remote effects occurred. Return to
  inspect and assign.
- `1`: operational error, such as missing session, unavailable or
  unauthenticated `gh`, or a push/PR failure. Fix the stated cause before
  retrying.

Do not loop indefinitely. Retry after a concrete local correction; ask the
author when the fix needs new credentials, a branch push, a changed base, or
another external mutation.

## Focus review feedback

For a task targeted at an existing slice, focus may act as a prior for genuinely
new atoms:

```bash
git-paoding focus storage
git-paoding status --json
git-paoding focus --clear
```

Focus must not overwrite confidently matched existing ownership. Clear it when
the targeted task ends so unrelated later work does not default to that slice.

## Recovery

- New, unassigned, ambiguous, or updated atoms after canonical edits are
  expected. Rerun `status`, inspect only what needs attention, assign it, and
  publish again.
- Renames and heavy rewrites may return as unassigned or ambiguous. That is safe
  degradation, not corruption.
- If local session metadata is lost, do not edit generated refs or PR bodies to
  reconstruct it. Reinitialize with the original pinned base and recreate the
  same stable slice IDs.
- Expect all atom attribution to return as unassigned after reinitialization. A
  clean publish can adopt existing open slice PRs through their machine markers.
- If a slice becomes empty, publication may leave its existing Draft PR open and
  mark it empty; do not invent changes merely to keep the projection nonempty.

If initialization pinned the wrong base and publication has not begun, use the
guarded replacement path instead of deleting metadata:

```bash
git-paoding init --replace --pr <correct-integration-pr-number-or-url>
# Or, when no integration PR exists:
git-paoding init --replace --base <correct-integration-target-branch>
```

Replacement first rejects any sign that generated refs or slice PR publication
may have started, then writes a timestamped exact metadata backup before
creating the new session. If it refuses, do not bypass the guard; ask the author
how to preserve the already-published review identity.

After GitHub reports the integration PR as merged, run:

```bash
git-paoding archive
```

`archive` closes slice PRs, retains their URLs and discussion history, and
cleans generated refs; it never merges them. It refuses to run while the
integration PR is still open or merely closed.
