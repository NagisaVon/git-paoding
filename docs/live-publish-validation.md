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
