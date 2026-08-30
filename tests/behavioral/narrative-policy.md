# Pull request narrative policy

New slice and integration pull requests contain only the machine-managed
region. The tool does not seed headings, prose prompts, checklists, or hidden
author instructions.

Agents and authors may add any useful review narrative outside the managed
delimiters. Every refresh preserves those outside bytes exactly while updating
only machine-owned metadata inside the delimiters.

The managed slice region retains its safety banner, integration link,
atoms-derived diffstat, related-slice links, stable identity marker, and any
lifecycle notes. The managed integration region retains only the slice index.
