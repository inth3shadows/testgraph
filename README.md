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

Pre-code. Dogfood-first against honeyslate and signedintake. Direction and
phases: `~/.claude/plans/testgraph-test-intelligence-mcp.md`.

## Shape

- CLI first (like codegraph), thin MCP wrapper second.
- Journey registry: named user journeys per repo, each mapped to entry
  symbols; CodeGraph expands symbol → impacted-journey sets.
- `testgraph plan` — diff in, risk-ranked journey list out.
- Results ledger — executions, failures, and root-cause notes feed the next
  plan (learn-from-failure).
- Consumer #1: the /verify gate in autonomous merge loops (forage/autorun) —
  agent-written code gets behavior-verified before merge.
