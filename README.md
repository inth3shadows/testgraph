# testgraph

Test intelligence above the tools that already exist: correlate what changed
(git diff), what it affects (CodeGraph edges), what actually runs (RunEcho /
runtime evidence), and what has been exercised (a behavior-coverage ledger) —
then hand a risk-ranked test plan to whatever executes tests (Playwright MCP,
Claude Code, CI).

## Positioning (from 2026-07-15 brainstorm)

The browser-driving layer is commoditized: Playwright ships Planner, Generator,
and Healer agents on top of its official MCP server. testgraph does NOT drive
browsers and does NOT generate Playwright scripts. Its moat is the layer no
driver has: a persistent map of user journeys ↔ code symbols ↔ last-exercised
evidence, and change-aware selection over that map ("this PR changed 6
functions; 2 were exercised; these 3 journeys are now high risk — run them").

## Status

PARKED 2026-07-15, pre-code — deferred in favor of a revenue-bearing project.
The direction below is recommended, not confirmed, and five unresolved holes
are recorded in the plan (chief among them: static call-graph
over-approximation may name so many journeys per change that the selector
degenerates to "run everything").

Everything needed to resume cold — holes, phases, scoring table, and the
cheapest next action (a selectivity probe over 5 honeyslate commits, hours not
days, which gates the whole project) — is in
`~/.claude/plans/testgraph-test-intelligence-mcp.md`. Dogfood-first against
honeyslate and signedintake.

## Shape

- CLI first (like codegraph), thin MCP wrapper second.
- Journey registry: named user journeys per repo, each mapped to entry
  symbols; CodeGraph expands symbol → impacted-journey sets.
- `testgraph plan` — diff in, risk-ranked journey list out.
- Results ledger — executions, failures, and root-cause notes feed the next
  plan (learn-from-failure).
- Consumer #1: the /verify gate in autonomous merge loops (forage/autorun) —
  agent-written code gets behavior-verified before merge.
