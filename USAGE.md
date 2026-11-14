# Usage rules for querying agents

Add to the team's `CLAUDE.md` or load as a skill:

1. Start with the index tools; they are cheap.
2. Always show the `provenance` source and commit in your answer.
3. Answer from the index alone when the question is a lookup the index holds as a fact (where a route, class or library is, which repos use something) and the repos involved are not stale. Say the answer is from the index and was not read from code.
4. Call `handoff` and spawn `repo-live-search` when any of these is true: results are empty or weak; the question needs code the index does not hold (request and response bodies, behaviour, whether two sides match); a result repo is stale; the answer will drive a design or reuse decision.
5. Never present `search_aid` text as fact. It only explains why a result ranked.
6. Report `unclassified`, `unknown`, `unresolved` and `UNKNOWN` severity as they are. Never fill them in.
