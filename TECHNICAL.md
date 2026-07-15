# Technical Reference: testgraph

Pre-code scaffold. This document records the planned architecture; it will be
rewritten as components land. Source of truth for scope, brainstorm scoring,
and phases: `~/.claude/plans/testgraph-test-intelligence-mcp.md` (private
plans store).

## Architecture (planned)

CLI first, thin MCP wrapper second (the codegraph precedent). Core is a
persistent behavior-coverage ledger: user journeys ↔ entry symbols ↔
impacted-symbol sets (expanded via CodeGraph edges) ↔ execution history
(last exercised, failures, root-cause notes).

`testgraph plan`: git diff → impacted symbols → impacted journeys →
risk-ranked list (fan-in, staleness of last exercise, failure history).
`testgraph record`: writes execution results back to the ledger.

Explicit non-goals: browser driving, test generation, screenshot diffing,
self-healing — all delegated to Playwright MCP and its Planner/Generator/
Healer agents, which commoditized that layer.

## Integrations (planned)

- CodeGraph (`.codegraph/` SQLite) — symbol → caller/callee expansion.
- RunEcho — live symbol truth for journey entry-point validation.
- git — diff input.
- Consumer #1: the /verify gate in forage/autorun agent-merge loops.

## Configuration, deployment, maintenance

Not yet applicable — no runnable code. Dogfood targets: honeyslate,
signedintake.

## Known limitations

Pre-code. Phase-0 validating experiment (8 hand-written honeyslate journeys
vs one real past PR) must beat "run everything" before the ledger is built;
otherwise pivot to trace-derived journey discovery.
