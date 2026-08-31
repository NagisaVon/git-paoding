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

Partition one committed `Base -> Final` diff into semantic Draft PR review
projections while keeping the canonical integration branch authoritative.

## Preserve the review model and authorization boundary

- Edit, test, lint, and commit only on the canonical integration branch.
- Treat slices as review units, not development phases, historical commit
  ranges, independently buildable units, or merge targets.
- Never check out, edit, rebase, restack, or manually force-push generated
  `paoding/...` branches. `publish` owns their deterministic regeneration and
  stable PR identities.
- Never merge a slice PR. Keep CI, approval, branch protection, and merge on the
  integration PR.
- Preserve human-written PR narrative outside git-paoding's managed regions.

Initialization, slice configuration, status, assignment, and focus are local
metadata operations, except that `init --pr` reads GitHub PR metadata. `publish`
pushes generated refs and creates or edits PRs; `archive` closes slice PRs and
deletes generated refs. Ask the author for authorization immediately before a
required canonical-branch push, `publish`, or `archive`. Authorization for one
mutation does not authorize later mutations.

## Initialize from the integration PR when it exists

Work from the local branch containing the complete committed change. Verify the
CLI first:

```bash
git-paoding --version
```

When an open integration PR exists, use its number or URL. This is the
recommended path:

```bash
git-paoding init --pr <integration-pr-number-or-url> --slice-prefix ABC-123
```

This read-only GitHub path accepts only an open, same-repository PR. It requires
the local PR head branch to match the PR head OID and both endpoint objects to
exist locally. It computes the local merge base and compares the resulting
rename-aware diffstat with GitHub. It never fetches. If validation reports stale
or missing local objects, follow its explicit fetch/checkout instruction, with
authorization for any network operation, then retry; do not guess another base.

A sandboxed authentication or connection failure from this read-only `gh`
operation is inconclusive. Retry the identical read-only command outside the
sandbox through the approval mechanism. Conclude that authentication is invalid
only if that elevated retry returns HTTP 401. Never expose tokens or start
`gh auth login` before that confirmation.

Only when no integration PR exists, initialize from an existing local or
remote-tracking branch:

```bash
git-paoding init --base <integration-target-branch> --slice-prefix ABC-123
```

`init --base` is local-only: it does not call GitHub or fetch, and it rejects an
OID, tag, or other non-branch base. Angle-bracket values are placeholders, never
defaults. Omit `--slice-prefix` to use `slice`; the prefix affects titles, not
slice IDs or generated refs. Either initialization path pins the base commit.

Add stable review concerns after initialization:

```bash
git-paoding slice add storage --title "Storage boundary"
git-paoding slice add tests --title "Storage behavior tests"
```

## Classify with one bounded workflow

Do not begin a large diff by loading every atom. Narrow from global counts to
paths and then to only the atom groups that need a decision:

```bash
git-paoding status --summary --json
git-paoding status --paths --action-needed-only --json
git-paoding status --path <exact-path> --path <related-path> --json
git-paoding status --path <exact-path> --full --json
git-paoding assign --batch paoding-assignments.json --quiet --json
git-paoding status --summary --json
```

- `--summary` returns global counts without paths or atoms.
- `--paths` returns preview-free path totals. `--path` selects exact paths and
  is repeatable for related atom groups.
- `--action-needed-only` filters paths or atoms to unresolved attribution.
- `--summary` and `--paths` are mutually exclusive. Repeatable `--path` is an
  atom view and cannot combine with either one. `--full` expands atom previews
  only, so it likewise cannot combine with `--summary` or `--paths`.
- Filtered views still use global unassigned and ambiguous counts for exit code
  `2`, so an empty filtered result can exit `2` while work remains elsewhere.

Exit code `2` means attribution needs a decision, not that the command failed.
Use atom IDs for precise selection; paths, directories, globs, and
Final-coordinate ranges select broader atom sets. A range such as
`path:20-45` selects whole intersecting atoms and never splits one. Inspect the
current Final file when a short preview lacks context; inspect Base for a
deletion with no Final lines.

Prepare one batch when the partition is known:

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

`--quiet --json` preserves every assignment record and identity key but leaves
each `preview` value as an empty string. Keep the batch file untracked. For a
small correction, interactive assignment remains available:

```bash
git-paoding assign storage <atom-id-or-selector>
```

Broad selectors claim only unassigned atoms unless `--force` is explicit; an
exact atom ID may take that atom directly. Batch reassignment uses the JSON
`force` field, never CLI `--force` with `--batch`. If a selector matches nothing
or an atom ID becomes stale after a commit, rerun the bounded status flow.

## Publish and refresh safely

Before publishing, confirm the canonical branch is available on the intended
remote. Obtain authorization immediately before any needed canonical push and
again immediately before this externally mutating command:

```bash
git-paoding publish --json --trace --network-timeout 120
```

Progress and trace output go to stderr; the human or JSON result stays on
stdout. Add `--quiet` to suppress progress while retaining the final result.
`--trace` reports aggregate phase and subprocess timings without arguments.
`--network-timeout <seconds>` bounds each Git or GitHub network process; `0`
disables the timeout.

Respond by exit code:

- `0`: clean success; report the integration PR and slice PRs.
- `2`: action needed and no remote effects. Resume with
  `status --summary`, `status --paths --action-needed-only`, and targeted
  `--path` views.
- `1`: operational failure. Correct the named cause before retrying; ask the
  author when that correction needs credentials, a branch push, or another
  external mutation.

Publication advertises generated refs once and applies changed refs as one
atomic push with an exact lease per destination. If it reports a concurrent
publisher, confirm only one publisher is active and rerun `publish`; never
override the lease or repair generated refs manually. If the remote rejects
atomic push, stop—the compatibility fallback is deliberately disabled.

`publish` is idempotent. Retry it after a concrete interruption or canonical
edit; an unchanged republish performs no ref push or PR edit. Apply review
feedback on the canonical branch, commit it, then repeat the bounded status flow
and publish with fresh authorization.

## Adjust or recover local review metadata

Use focus only as a prior for genuinely new atoms during work targeted at one
slice, then clear it:

```bash
git-paoding focus storage
git-paoding focus --clear
```

Inspect or revise slice identities locally with `slice list`, `slice rename`,
and `slice remove`. Rename preserves identity. Remove returns owned atoms to
unassigned; a later authorized publish closes its open slice PR and cleans its
generated refs.

If initialization pinned the wrong base and publication has not begun, replace
the session through the same PR-first decision:

```bash
git-paoding init --replace --pr <correct-integration-pr-number-or-url>
# Only when no integration PR exists:
git-paoding init --replace --base <correct-integration-target-branch>
```

Replacement refuses any durable or local sign that publication started and
writes a timestamped exact metadata backup before replacing the session. Do not
bypass a refusal; preserve already-published review identity and ask the author
how to proceed.

After ordinary canonical edits, unresolved or updated atoms are expected; use
the bounded flow again. Renames and heavy rewrites may safely degrade to
unassigned or ambiguous. If metadata is lost, never reconstruct it by editing
generated refs or PR bodies. Reinitialize from the original pinned source,
recreate the same stable slice IDs, reassign, and let an authorized publish
adopt matching open PR markers.

## Archive only after the integration PR merges

After GitHub reports the integration PR as merged, obtain authorization and run:

```bash
git-paoding archive
```

`archive` closes slice PRs without merging them, retains their URLs and review
history, and deletes generated refs. It refuses while the integration PR is
open or merely closed.

## Library callers: decision-changing interfaces

CLI workflows should use the commands above. Library integrations should choose
the corresponding typed entry points:

- `init_session_from_pr()` validates a resolved `PullRequestTarget` through the
  PR-first rules. `init_session()` is the local-base path; its deprecated
  `backend=` keyword is ignored.
- `replace_session()` is the only supported wrong-base replacement path and
  returns the backup path with refreshed status.
- `get_status_view()` returns additive `StatusViewResult` v0 for summary, path,
  or filtered atom views. Unfiltered `get_status()` remains the frozen
  `StatusResult` v0 contract.
- `publish()` accepts a progress callback and per-process network timeout for
  non-CLI frontends.

The assign and publish JSON contracts remain v0, and session storage remains
schema v1. Do not infer permission for remote effects from calling a local API.
