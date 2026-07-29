# Technical Reference: testgraph

Phase-1 spike. This documents the built selector; the broader roadmap (ledger,
MCP wrapper, trace discovery) lives in the private plan
`~/.claude/plans/testgraph-phase1-graph-traversal-spike.md`.

## Architecture

testgraph reads a CodeGraph index (`.codegraph/codegraph.db`, a SQLite graph of
`nodes` and `kind`-typed `edges`) **directly** — it does not shell out to the
`codegraph` CLI, whose `impact` command returns only immediate dependents plus a
file-level cross-file bucket, not the transitive symbol closure this needs.

Data flow for `testgraph.select`:

```
git diff --unified=0 base..head
  -> changed (file, line-range) pairs        [select.changed_ranges]
  -> seed nodes: nodes whose [start,end] overlaps a changed range   [db.nodes_for_lines]
  -> impacted set: reverse-transitive closure over the graph        [db.impacted_closure]
  -> journeys whose entry symbols are in the impacted set           [registry.resolve_entries]
  -> ranked by entry fan-in
```

Two closure propagation rules, both load-bearing for recall (validated on
honeyslate):

1. **Reverse reachability** over edge kinds `calls`, `references`,
   `instantiates`, `extends`, and `imports` — "who depends on this."
2. **File expansion** — when a `file:` node enters the closure, every symbol it
   `contains` is added. This is required because the module-level singleton
   idiom (`_settings = get_settings()`) is recorded by CodeGraph *only* as an
   `imports` edge from the file node, never as a `calls` edge from the reading
   function. Without rule 2 the closure dead-ends at the file node and silently
   drops every journey that reads a shared module global.

`imports` is deliberately included: it is the only edge connecting a consuming
module to a shared symbol under that idiom. The cost is over-selection on
shared-symbol edits (a config change touches every importer) — an accepted
recall-first trade, not a bug.

## The integrity guard (`integrity.py`)

Runs before any selection and blocks if the index can't be trusted. Motivated
by a real incident: an interrupted `codegraph` run left blast radius 85% wrong,
and `codegraph sync` did NOT repair it (it cleared the pending-ref warning while
leaving edges wrong) — only a full `codegraph index` did. Three checks:

1. **Pending refs** — `unresolved_refs.status = 'pending'` above threshold means
   the index is mid-resolution. (Terminal `failed` refs are external stdlib and
   ignored.) Blocking.
2. **Freshness** — any tracked source newer than its `files.indexed_at` row.
   Warning only (degrades precision, not recall).
3. **Caller-count spot-check** — for pinned symbols (e.g. `get_settings`), the
   direct inbound-edge count must meet a floor. This is the check the pending/
   freshness checks miss: it catches the sync-does-not-repair corruption class.
   Blocking; remedy printed is `codegraph index` (never `sync`).

## File Descriptions

- `testgraph/db.py` — connection, schema-version read, line→node mapping, the
  recursive-CTE `impacted_closure`, `caller_edge_count`, symbol resolution.
- `testgraph/integrity.py` — the three-check guard; returns `(blocking, warnings)`.
- `testgraph/registry.py` — loads the journey JSON, resolves entry `name`+`file`
  to node ids at run time (ids are stable, names drift), maps every matching
  node to its journey.
- `testgraph/select.py` — diff parsing (test/e2e files excluded as seeds),
  orchestration, ranking, human + `--json` output, the CLI.
- `journeys/honeyslate.json` — 8 journeys (submit, browse, edit, reschedule,
  comments, auth, gcal sync, scheduler) + `spot_checks` floors for the guard.
- `harness/accuracy.py` — checks each labeled commit out in an isolated worktree
  off `.bare`, builds its own index there, runs the selector for that commit,
  scores recall/precision against `harness/labels_honeyslate.json`.
- `tests/test_core.py` — closure + guard unit tests on a synthetic in-memory db.

## Integrations

- **CodeGraph** — read-only against `.codegraph/codegraph.db`. Schema is a
  codegraph-internal contract, not a public API (see limitations).
- **git** — `git diff` for input; `git worktree` (harness only) for per-commit
  indexing.

## Configuration

- `--repo` (default honeyslate/main), `--base`/`--head` (default `HEAD~1`/`HEAD`),
  `--db` (default `<repo>/.codegraph/codegraph.db`), `--registry`, `--json`.
- Registry `spot_checks`: `{symbol: {min_caller_edges, file}}` — the guard floors.

## Deployment

No service to deploy — a CLI run against a target repo's index. Intended
consumers: a CI gate reading `--json`, or (future) an MCP wrapper feeding the
`/verify` step of autonomous merge loops.

## Maintenance Commands

```bash
python3 -m unittest tests.test_core     # unit tests (no codegraph needed)
python3 harness/accuracy.py             # recall/precision on labeled commits
codegraph index <repo>                  # rebuild a target index (NOT sync, on corruption)
```

## Seeded-Regression Eval (issue #5)

`harness/seed_regressions.py` manufactures the hard cases the 5 hand-labeled
commits don't cover. It checks honeyslate out once, indexes once, then for each
of ~20 sampled functions edits one line *inside* that function, commits, and asks
the selector which journeys the change endangers. The edit preserves the
function's line count, so the base index stays aligned and no re-index is needed
per site.

Ground truth is `harness/ast_oracle.py` — a call graph built from source text by
Python's own `ast`, walked **forward** from each journey entry, whereas the
selector walks **backward** from changed symbols. Same relation, independently
derived; a shared missing edge cannot hide in both.

**It measures selection, not detection.** Whether a behavioral mutation would
actually fail a journey needs runnable journeys (issue #8's environment). To the
selector, a seeded diff and a seeded bug are identical — it only reads the diff.

**Adjudication.** The oracle matches calls by bare name, so it over-approximates
by construction. A disagreement is therefore a *question*, not a verdict: each is
recorded in `harness/adjudications.json` as `oracle-false-positive` (with the
call-path evidence) or `selector-miss` (a real recall bug — fix the closure).
Unadjudicated disagreements fail the run deliberately, and the oracle is never
tuned to agree.

Current result: **20 sites, adjudicated recall 1.00, mean worst rank 3.17, mean
3.33 of 8 journeys named.** Three disagreements, all adjudicated as oracle false
positives with sole-caller evidence.

## Schema Pin (R1)

`integrity.check()` reads the index's `schema_versions` row and compares it to
`codegraph_schema_version` in the registry (currently `8`). Mismatch, or a pin
with no row present, is **blocking** — codegraph's SQLite layout is an internal
contract, so a renamed column would make the closure query return wrong rows
rather than error. No pin in the registry downgrades to a warning that names the
version to add.

## Whole-File Changes (deletions and renames)

Deletions and renames carry no usable line ranges, and both used to vanish
silently: a deletion's `+++` header is `/dev/null` (not a `.py` path, so the file
was dropped), and a pure rename emits no `@@` hunks at all. `git diff` runs with
`-M` so a move is read as a rename rather than an unrelated delete + add.

Each such path is seeded with *every* symbol the file contains. When the path is
absent from the index — the normal case for a file deleted in `head` — its impact
is **unbounded**, so the result sets `recall_degraded: true`, warns, and lists
every registered journey with `verify_manually: true`. Degrading toward "test
everything" is the recall-first answer; degrading toward silence is the failure
mode the whole project exists to prevent.

## Path Confidence (B1)

Each `calls`/`imports`/`references` edge carries `metadata.confidence` (observed
range 0.5–1.0 on honeyslate). The closure propagates it as `max over paths of
(min over edges)` inside the recursive CTE, carrying a `conf` column and
`GROUP BY id` with `max(conf)` at the end. Termination is unaffected: the
confidence domain is finite and `min` is monotone, so the `(id, conf)` pair space
is finite and `UNION` still converges.

- Missing `confidence` → `DEFAULT_EDGE_CONFIDENCE = 0.9`. Deliberately high:
  confidence annotates, never filters, so guessing low on an unmeasurable edge
  would only manufacture false warnings.
- `provenance='heuristic'` (synthesized JSX/dynamic-dispatch edges) → capped at
  `0.3` whatever the metadata claims.
- `contains` file-expansion inherits the file node's confidence — containment is
  structural, not an inference hop.
- A journey at or below `LOW_CONFIDENCE = 0.6` renders `VERIFY MANUALLY` and sets
  `verify_manually: true` in `--json`.

**Note on `edges.provenance`:** the parent plan proposed keying B1 on this
column. Measured across 17 indexed repos it is binary (`NULL` / `'heuristic'`)
and honeyslate has *zero* heuristic Python edges, so a provenance-only version
would flag nothing. `metadata.confidence` is the mechanism that carries signal;
the heuristic cap is retained because it starts earning once TS/JSX journeys land.

## Known Limitations

- **Scope:** honeyslate + Python only. Other repos need their own journey
  registry; TS/JS frontend journeys are out (needs the frontend indexed).
- **Precision on shared symbols:** a config/model edit fans out to most journeys
  by design (recall-first). Mean precision 0.84 on the labeled set; the low case
  is 0.38 (a `Settings` field change → all 8).
- **Registry completeness is manual:** a journey's entry set must include its
  wiring/lifecycle code, not just the leaf handler (the harness caught J8 missing
  `scheduler.start`). Missing entries cause silent under-selection.
- **Index integrity is the tool's soundness ceiling.** The guard mitigates but
  cannot fully verify a graph; a subtly wrong index yields wrong answers.
- **Deleted files absent from the index cannot be bounded.** A file deleted in
  `head` is normally gone from an index built at `head`, so its former
  dependents are unknowable. testgraph degrades to listing every journey with
  `recall_degraded: true` rather than answering narrowly — correct, but useless
  for selectivity on such commits. A base-time index (as the harness builds)
  resolves them properly.
- **Confidence rarely fires on honeyslate's labeled commits.** The mechanism is
  live (a weak-edge seed propagates ≤0.6 through 149 nodes on the real index),
  but all 5 harness commits reach their journey entries via 0.9 routes, so no
  `VERIFY MANUALLY` flag appears there. Its value is on shared/dynamic code
  paths, and it will matter more once TS/JSX journeys are indexed.
