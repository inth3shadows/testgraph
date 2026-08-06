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
handlers, the scheduler sweep, etc.). `testgraph.propose` drafts one for a new
repo by scanning Python route decorators and Next.js conventions against the index, marking it `approved: false`
until a human reads it — an unapproved registry runs, loudly, but never silently.
For a diff, testgraph:

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

# Draft a registry for a repo that has none (then review it — see the skill):
python3 -m testgraph.propose --repo <path>

# Wire it into every repo with an approved registry, so pushes answer by themselves:
hooks/install.sh

# Run the tests and the accuracy harness:
python3 -m unittest discover -s tests
python3 harness/accuracy.py            # 5 hand-labeled real commits
python3 harness/seed_regressions.py    # ~20 seeded mutation sites

# Score the static footprint against what a journey actually EXECUTES (issue #12).
# Needs a target whose own test suite runs; see harness/fixtures/dyndemo/README.md.
python3 harness/trace.py --repo <target> --python <its venv python> \
    --tests tests --root app --out traces/<target>.json
python3 harness/ground_truth.py --trace traces/<target>.json \
    --map harness/journey_tests_<target>.json \
    --registry journeys/<target>.json --db <target>/.codegraph/codegraph.db
```

## Wiring It In

A selector nobody calls cannot be wrong, and cannot be learned from. `hooks/install.sh`
installs a `pre-push` hook into every repo with an approved registry, so each push
prints the journeys it could have broken:

```
testgraph[signedintake]: 11 journey(s) this push could break, d9174b7e1..0305ada6f, ranked:
  [ 23] J1  claimant submits a signed form  (conf 1.0)
  [ 16] J10  staff regenerates and downloads the signed PDF  (conf 1.0)
  …
```

`pre-push` is the only hook git hands a real base: it receives
`<local ref> <local sha> <remote ref> <remote sha>` on stdin, and the remote sha is
exactly the "what they don't have yet" boundary the question wants. A first push of a
new branch has no such boundary, so the hook falls back to the merge-base with the
repo's base branch — six commits' worth of change, not just the tip.

**It never fails a push.** Every path exits 0 — a blocked integrity guard, a missing
index, a missing registry, a traceback, a timeout. Advice that can stop a push stops
being advice; it gets uninstalled. Opt out per repo with
`git config testgraph.enabled false`, or remove it with `hooks/install.sh --uninstall`.

It runs `codegraph sync` first. Seeds come from *line ranges*, so an index built
before the code moved resolves the diff against stale spans, and nothing else
refreshes these indexes on commit. If a changed file's bytes still disagree with the
indexed copy afterwards, the answer degrades to `RECALL DEGRADED` and names the file
instead of quietly trusting the wrong line numbers.

Each run appends one line to `~/.local/share/testgraph/invocations.jsonl`
(`{ts, repo, base, head, status, n_journeys, journey_ids, duration_ms, caller}`), which
is how "is anything actually calling this?" gets answered with a number instead of a
guess. That is the invocation log, not the results ledger — it records what the
selector *said*, not what running the journeys then *found*.

## Project Structure

- `testgraph/` — the package: `db.py` (graph traversal), `integrity.py` (the
  guard), `registry.py` (journey resolution), `select.py` (the CLI).
- `journeys/honeyslate.json` — the hand-authored journey registry for the first
  dogfood target.
- `harness/` — `accuracy.py` (recall/precision on labeled commits),
  `selectivity.py` (selection sizes per commit for a target with no labels),
  `seed_regressions.py` + `ast_oracle.py` (seeded-mutation eval against an
  independent oracle), `adjudications.json` (hand-ruled disagreements),
  `trace.py` + `tgtrace.py` + `ground_truth.py` (score the static footprint
  against what a journey actually *executes*, issue #12).
- `hooks/` — `pre-push` (the git hook that runs the selector on every push) and
  `install.sh` (installs it into each repo with an approved registry).
- `maps/` — generated journey maps (symbol -> journeys, grouped by file).
- `skills/testgraph-verify/` — the agent skill that reads a map pre-commit.
- `skills/testgraph-propose/` — the agent skill that reviews a drafted registry:
  groups handlers into journeys, closes the declared blind spots, approves.
- `tests/` — unit tests over a synthetic fixture (no CodeGraph needed).

## Status

Phase-1 spike plus B1 (confidence-weighted paths), working and validated on
honeyslate: recall 1.00 across 5 hand-labeled commits (mean precision 0.68, down
from 0.84 when frontend files began being seeded — see TECHNICAL.md) and 1.00
across 20 seeded mutation sites scored against an independent AST oracle;
integrity guard tested and schema-pinned. Scoped to honeyslate; backend and
frontend files are both analysed, journeys are registered on backend entry points.
The gap was not capability, it was **consumption**: `skills/testgraph-verify` had
never been invoked — 0 times across every session on this machine, which is what
issue #49 measured. The `pre-push` hook above is the answer to that: it calls the
selector whether or not anyone remembers to, and logs each call so the next
increments (the results ledger, the `/verify` gate) have real runs to build on
instead of an assumed caller. Whether it worked is a number, not an opinion —
`wc -l ~/.local/share/testgraph/invocations.jsonl` a week from install.

**On whether this is worth it:** at 8 journeys selection avoids 57.5% of journey-runs
on the labeled set, but bimodally — 3 of 5 commits select ≤2 journeys, 2 of 5 select
≥6. The case for the tool at this scale is the *target* it gives an agent and the
guards that refuse a confident wrong answer, not the runtime saved.

**That was stated as a falsifiable claim, and in 2026-08 it was falsified.** Measured on
two repos with no registry before — one of them written by strangers — the bimodality
holds at 20+ journeys and the "total" pole does not recede: mealie at 23 journeys gives a
histogram of literally `{0: 38 commits, 23: 2 commits}`, and coriolis-local at 207
journeys avoids only 54.1% of journey-runs once commits that touch no registered surface
are excluded. **Read the selection numbers here as a floor on coupling, not a promise of
savings.** What the same measurement *did* confirm is the ranking: a false-positive
whole-registry selection came back flagged `verify_manually` at confidence 0.3 while a
genuine 82-journey blast radius came back clean at 0.9. TECHNICAL.md "Update 3" has the
full result, the method, and the two corrections it forced to earlier figures.

## Related Documentation

- [Technical Reference](TECHNICAL.md) — architecture, the traversal, the guard, limitations.
- [Usage Guide](USAGE.md) — how to run it and read its output.

Design plan: `~/.claude/plans/testgraph-phase1-graph-traversal-spike.md`.
