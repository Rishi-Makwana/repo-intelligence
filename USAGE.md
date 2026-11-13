# Usage rules for querying agents

Add to the team's `CLAUDE.md` or load as a skill:

1. Start with the index tools; they are cheap.
2. Always show the `provenance` source and commit in your answer.
3. Call `handoff` and spawn `repo-live-search` when any of these is true: results are empty or weak; the question is about code-level detail; a result repo is stale; the answer will drive a design or reuse decision.
4. Never present `search_aid` text as fact. It only explains why a result ranked.
5. Report `unclassified`, `unknown`, `unresolved` and `UNKNOWN` severity as they are. Never fill them in.
