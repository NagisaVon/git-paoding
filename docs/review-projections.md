# Reviewing git-paoding projections

<!-- cspell:words paoding -->

A slice pull request is a view onto one semantic part of the final integrated
change. Different slices may touch different regions of the same file, and one
slice may rely on code shown by another. A slice is not required to build or
test by itself.

Every slice PR is a Draft and carries a **DO NOT MERGE — review projection
only** warning because its generated base and head refs are disposable. Use
normal GitHub review features to inspect the change and leave comments, but keep
authoritative CI, approval, branch protection, and merge on the integration PR.
After feedback, the author updates the canonical branch and refreshes the same
slice PR; GitHub may mark comments outdated when their lines change, while the
discussion and stable PR identity remain available.

## CI configuration

Slice projections are review units, not integration units. In consumer
repositories, filter `pull_request` workflows to real target branches so
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

GitHub evaluates `pull_request.branches` against the PR's base branch. Keep the
normal CI and branch-protection requirements on the integration PR.

After GitHub reports the integration PR as merged, ask the agent to archive the
review projections. The installed skill contains the archive workflow and its
safety checks.
