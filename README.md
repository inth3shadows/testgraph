# testgraph

Journey-level test selection. Given a git diff, testgraph answers "which
user-facing flows could this change have broken, and in what order should they
be tested?" — reading the change, walking a CodeGraph index of how the code
connects, and returning a short ranked list instead of "re-run everything."

It sits above the tools that already exist: it does NOT drive browsers, generate
tests, or self-heal (Playwright's Planner/Generator/Healer agents commoditized
that). Its job is the layer no driver has — deciding *what is worth testing*.

## How It Works

A **journey registry** names each user journey and its entry symbols (route
handlers, the scheduler sweep, etc.). For a diff, testgraph:

1. maps changed line ranges to the symbols that own them (the *seeds*);
2. walks the CodeGraph edge graph in reverse — transitively — to every symbol
   that depends on a seed (the *impacted set*);
3. reports the journeys whose entry symbols fall in that set, ranked by fan-in,
   each carrying the **confidence** of the strongest edge-path that reached it.

Confidence is `max over paths of (min over edges)` — a chain is only as
trustworthy as its weakest hop, but one solid route is enough. A journey reached
only through weak or synthesized edges is flagged `VERIFY MANUALLY` rather than
silently trusted. It never removes a journey from the selection.

It is **recall-first**: it would rather over-select (flag a journey that turned
out fine) than silently drop a journey a change really did affect. A shared
config edit therefore fans out to many journeys on purpose.

Before answering, an **integrity guard** refuses to run off a corrupted or stale
CodeGraph index — because a wrong graph produces a confidently-wrong "you don't
need to test that" answer, the one failure mode a test selector must never have.

## Prerequisites

- Python 3.11+ (standard library only — no third-party dependencies).
- A target repo with a CodeGraph index (`.codegraph/codegraph.db`). Build one
  with `codegraph init <path>`.
- `git` (diff input).

## Quick Start

```bash
# From the testgraph repo root, against a CodeGraph-indexed target:
python3 -m testgraph.select \
  --repo /home/ericm/personal_projects/honeyslate/main \
  --base HEAD~1 --head HEAD

# JSON output (for a CI gate or another agent to consume):
python3 -m testgraph.select --repo <path> --json

# Export the static journey map an agent reads pre-commit:
python3 -m testgraph.export --repo <path> --out maps/honeyslate.md

# Run the tests and the accuracy harness:
python3 -m unittest tests.test_core
python3 harness/accuracy.py            # 5 hand-labeled real commits
python3 harness/seed_regressions.py    # ~20 seeded mutation sites
```

## Project Structure

- `testgraph/` — the package: `db.py` (graph traversal), `integrity.py` (the
  guard), `registry.py` (journey resolution), `select.py` (the CLI).
- `journeys/honeyslate.json` — the hand-authored journey registry for the first
  dogfood target.
- `harness/` — `accuracy.py` (recall/precision on labeled commits),
  `seed_regressions.py` + `ast_oracle.py` (seeded-mutation eval against an
  independent oracle), `adjudications.json` (hand-ruled disagreements).
- `maps/` — generated journey maps (symbol -> journeys, grouped by file).
- `skills/testgraph-verify/` — the agent skill that reads a map pre-commit.
- `tests/` — unit tests over a synthetic fixture (no CodeGraph needed).

## Status

Phase-1 spike plus B1 (confidence-weighted paths), working and validated on
honeyslate: recall 1.00 across 5 hand-labeled commits (mean precision 0.84) and
1.00 across 20 seeded mutation sites scored against an independent AST oracle;
integrity guard tested and schema-pinned. Scoped to honeyslate and Python. Next increments (the results ledger, an
MCP wrapper, the `/verify` gate) are in the plan.

## Related Documentation

- [Technical Reference](TECHNICAL.md) — architecture, the traversal, the guard, limitations.
- [Usage Guide](USAGE.md) — how to run it and read its output.

Design plan: `~/.claude/plans/testgraph-phase1-graph-traversal-spike.md`.
