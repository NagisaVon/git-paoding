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

> **Draft:** This skill is pre-release until the final dry-run. Commands marked
> **integration draft** are pending CLI integration and must be checked against
> `git-paoding --help` before use.

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
  projection only**. Authoritative CI, approval, branch protection, and merge
  belong to the integration PR.
- Do not require slice-level CI or independent buildability. Cross-slice
  dependencies are valid.
- Preserve human-written PR narrative outside machine-managed regions.

The operating rule is: **run `git-paoding publish` and do what it says**.
Publication is idempotent and self-checking; there is no separate refresh
ritual.

## Plan for review without bookkeeping every edit

When review concerns are already apparent, use them as provisional,
low-frequency structure guidance:

- prefer one primary review concern per new module or test file when that is a
  natural boundary;
- separate meaningfully different test concerns when ordinary readable
  organization permits; and
- avoid one large contiguous insertion that mixes unrelated concerns when a
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

Work from the branch containing the complete committed change. Confirm `gh`
2.45.0 or newer is authenticated and that the canonical branch is available on
the Git remote. Do not push a branch without the author's authorization.

```bash
gh auth status
git-paoding init --base origin/main
git-paoding slice add storage --title "Storage boundary"
git-paoding slice add tests --title "Storage behavior tests"
```

The base is pinned to a commit at initialization. Choose the real integration
target; do not silently change it later.

## Classify and publish

### 1. Inspect

```bash
git-paoding status --json
```

Interpret exit code `2` as **action needed**, not an operational failure. Read
the versioned JSON atom list. Use atom IDs as the safest precise selectors.
Path, glob, directory, and range selectors are an **integration draft** until
the completed selector CLI lands.

Line ranges use Final coordinates: `path:20-45` means lines 20 through 45 in the
file at the canonical branch tip. A partial range selects the whole atom; ranges
do not split atoms.

Previews are short. If a preview is insufficient, read the current file at
`final_start` through `final_start + final_len - 1` before assigning; do not
guess from the preview. For a deletion or other atom with no Final lines,
inspect the relevant Base-side change instead.

### 2. Assign — integration draft

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

This exact batch invocation is **pending final CLI verification**. On the
current main branch, use the implemented form instead:

```bash
git-paoding assign storage src/storage.py
git-paoding assign tests tests/test_storage.py
```

Review the assignment echo: it must list every moved or skipped atom with a
preview. Path and range selectors should claim only unassigned atoms by default.
Reassignment requires an explicit override; the final `--force` spelling is also
pending CLI verification. If a selector matches nothing or an atom ID became
stale after a new commit, rerun `status --json` rather than guessing.

### 3. Publish

```bash
git-paoding publish
```

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

## Focus review feedback — integration draft

For a task targeted at an existing slice, focus may act as a prior for genuinely
new atoms:

```bash
git-paoding focus storage
git-paoding status --json
git-paoding focus --clear
```

These commands are **pending final CLI verification**. Focus must not overwrite
confidently matched existing ownership. Clear it when the targeted task ends so
unrelated later work does not default to that slice.

## Recovery

- New, unassigned, ambiguous, or updated atoms after canonical edits are
  expected. Rerun `status`, inspect only what needs attention, assign it, and
  publish again.
- Renames and heavy rewrites may return as unassigned or ambiguous. That is safe
  degradation, not corruption.
- If local session metadata is lost, do not edit generated refs or PR bodies to
  reconstruct it. Reinitialize with the original pinned base and recreate the
  same stable slice IDs. All atom attribution returns as unassigned; a clean
  publish can adopt existing open slice PRs through their machine markers.
- If a slice becomes empty, publication may leave its existing Draft PR open and
  mark it empty; do not invent changes merely to keep the projection nonempty.

After the integration PR merges, the planned command is:

```bash
git-paoding archive
```

`archive` is an **integration draft pending final CLI verification**. It should
close slice PRs, retain their URLs and discussion history, and clean generated
refs; it must not merge them.

## Validate this draft

From the `git-paoding` repository, run the isolated documentation smoke:

```bash
python3 docs/smoke_doc_commands.py
```

Once batch, focus, lifecycle, and archive commands land, run the strict gate:

```bash
PAODING_REQUIRE_FINAL_CLI=1 python3 docs/smoke_doc_commands.py
```
