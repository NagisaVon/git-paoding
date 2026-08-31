# Live publish workflow validation

This manual workflow validates the end-to-end `init` → `slice add` → `assign` → `publish` path
against real Git and real GitHub. It is not a CI test: it creates external resources and
requires an authenticated `gh` account with permission to create private repositories.

Run it from the `git-paoding` checkout:

```bash
uv run python scripts/live_publish_validation.py \
  --evidence /tmp/git-paoding-live-publish-evidence.json
```

The script always creates a new, clearly named private repository under the current `gh`
account. It pushes only that repository's canonical and generated refs, creates Draft slice and
integration PRs, and adds one inline review comment. It never deletes the GitHub repository;
the final URL is printed so the author can inspect the retained evidence. A failed run also
prints and preserves the repository URL.

The five checked stages are:

1. The real CLI performs `init`, `slice add`, `assign`, and `publish`; Draft slice/integration
   PRs, markers, the warning banner, merge-base, exact visible diff, empty-slice behavior, and
   exit codes are asserted.
2. An unchanged republish must retain byte-identical ref OIDs and exact PR/body/timeline
   snapshots.
3. A canonical Slice A edit must force-push both generated refs, refresh the same PR number,
   and expose the updated exact diff.
4. Deleting only the scratch repository's `.git/paoding/` metadata must degrade to unassigned;
   re-init/assign/publish must adopt the original PR by marker without a duplicate.
5. An inline comment is placed on Slice A, then a Slice B hunk is added in the same file. The
   full-Final refresh must rewrite Slice A's refs while preserving its PR number, exact visible
   patch, and live inline-comment anchor.

The JSON evidence contains the scratch repo/PR URLs, every important ref/OID, timeline and body
fingerprints, marker recovery, comment identity/anchoring, the 12 invariant walk, validated
empty-slice, integration-PR, and exit-status behavior, and the frozen interface contract scope.

The accepted 2026-08-29 run is recorded in
`docs/evidence/live-publish-validation-2026-08-29.md`.

## Atomic two-ref push probe

The script also has an isolated capability-probe mode for an owner-authorized existing
validation repository. The target must be private, unarchived, match the exact `OWNER/NAME`
argument, and retain the description created by this script. Unlike the five-stage workflow,
this mode does not create a repository or any pull requests.

Run it only after the repository owner separately authorizes the live probe:

```bash
uv run python scripts/live_publish_validation.py \
  --atomic-push-probe \
  --probe-repo OWNER/git-paoding-live-publish-YYYYMMDD-HHMMSS \
  --evidence docs/evidence/atomic-push-probe-YYYY-MM-DD.json
```

The probe resolves two existing commits from the source checkout and uses a random namespace
under `refs/heads/git-paoding-probes/atomic-push/`. It first creates two throwaway refs with one
`git push --atomic`, observes their exact remote OIDs, then swaps their desired OIDs with a
second atomic push carrying an exact `--force-with-lease=<ref>:<observed-oid>` for each ref.
Canonical branches and `refs/heads/paoding/` generated refs are never destinations.

On success or failure, cleanup re-reads each probe ref and deletes it only when its OID is one
of the two expected source commits, using an exact lease for each deletion. A changed or
unrecognized ref is left untouched and reported as a cleanup failure. The JSON evidence file is
required to be a new file directly under `docs/evidence/`; it records only sanitized operation
names, timestamps, return codes, observed/desired/final OIDs, capability outcome, and cleanup
status. Commands, credentials, raw remote URLs, stdout, and stderr are not persisted or printed.

A fully successful probe supports keeping the atomic-push fallback disabled. The probe records
that conclusion but never changes the fallback constant itself.

## v0.1.2 release validation

The release mode is separate from the historical five-stage workflow and never creates or
selects a repository on its own. Run it only after the owner authorizes one exact existing
private scratch repository. The repository must be unarchived and retain the scratch description
shown above. The mode refuses any other target and requires a new JSON evidence path directly
under `docs/evidence/`.

The owner-approved command is:

```bash
PYTHONPATH=src /Users/chang/Documents/git-paoding/.venv/bin/python \
  scripts/live_publish_validation.py \
  --release-validation \
  --release-repo OWNER/NAME \
  --baseline-pre-pr-seconds PREVIOUS_SAMPLE_1 \
  --baseline-pre-pr-seconds PREVIOUS_SAMPLE_2 \
  --baseline-pre-pr-seconds PREVIOUS_SAMPLE_3 \
  --evidence docs/evidence/live-release-v0.1.2-2026-08-31.json
```

Replace every placeholder with an owner-reviewed value before authorization. Baseline samples
are optional historical measurements; repeat the option for each comparable sample. Without
them, the evidence records the current preparation median but leaves the non-blocking 3× target
unevaluated.

The command validates the exact target with a read-only `gh repo view`, then writes only within
that scratch repository: two unique validation branches, one real Draft integration PR, fourteen
generated projection refs, seven Draft slice PRs, and the integration PR's managed slice index.
It exercises `init --pr` against that open integration PR, publishes the 315-directory,
33-changed-file, 36-atom, seven-slice field shape, and performs an unchanged republish. The
republish must report seven no-op outcomes, exactly one Git remote process (the required
`ls-remote`, therefore no push), zero GitHub writes, and an unchanged hash of all open PR
snapshots.

The evidence records first-progress latency, all eight phase timings, aggregate subprocess
counts, the longest interval between visible progress events, the pre-PR preparation median and
optional reduction factor, the `init --pr` URL/result, and no-op ref/PR checks. The one-second,
3×, and silent-interval observations are non-blocking targets; missing no-op guarantees or an
incomplete trace fail the run. The scratch branches and PRs are preserved for owner audit.

The atomic capability result is already recorded in
`docs/evidence/atomic-push-github-2026-08-31.json`: GitHub accepted the two-ref atomic exact-lease
update, cleanup left no probe refs, and the per-slice fallback remains disabled. Do not overwrite
or rerun that evidence as part of the release validation.
