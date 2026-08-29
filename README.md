# git-paoding

Coding agents are good at making one large, coherent change across a whole repository. Humans are not good at reviewing one: a 2,000-line, 20-file pull request is exhausting to read even when the code is correct. The usual fix, stacked PRs, creates a worse problem for agents: once review feedback changes an early design decision, the agent has to keep an entire stack of dependent branches semantically consistent through rebases and cross-branch edits, which is exactly what agents are bad at.

`git-paoding` splits the review instead of the work. The agent keeps everything on **one integration branch**, and when the change is ready for human eyes, the tool cuts the final diff into **semantic slices** and opens each slice as a small Draft GitHub PR with its own writeup. Reviewers read a few focused PRs of a comfortable size. Building, CI, approval, and merging all stay on the integration branch; a slice PR exists only to be read and understood. When review feedback changes the design, the agent edits the integration branch as it normally would, and the slice PRs are regenerated to match. Nobody maintains a branch stack.

The name comes from 庖丁解牛 (*Chef Ding carves the ox*): the butcher who cuts along the natural joints, so the blade never dulls.
