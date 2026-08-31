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
