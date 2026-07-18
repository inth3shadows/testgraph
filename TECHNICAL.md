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
- **Schema coupling:** reads codegraph's SQLite columns directly, so a codegraph
  upgrade could break it. Pin/verify `schema_versions` before trusting output.
- **Deleted files / renames** are not mapped as seeds in the spike.
