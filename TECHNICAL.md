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
2. **Freshness** — any tracked source newer than its `files.indexed_at` row, in
   **every** indexed language. Warning only (degrades precision, not recall). The
   query filtered `language = 'python'` until issue #31: harmless while non-Python
   paths could not affect selection, wrong once they could — a frontend file edited
   after the last index gave a narrow answer with no staleness warning.
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

## Static Journey Map (issue #9)

`python -m testgraph.export` writes the selector's **reverse index** — symbol →
journeys that depend on it, grouped by file — as markdown plus JSON. An agent
mid-edit then needs no CodeGraph index, no Python, and no MCP call: it looks up
the lines it changed and reads off the journeys.

Rows are computed by running the *same* `impacted_closure` the selector runs,
seeded with one symbol at a time, and asking which journey entries land in it. A
faster inverse traversal would risk disagreeing with `select`, and a map that
disagrees is worse than no map because an agent would trust it. Runtime is ~0.3s
for honeyslate's 644 nodes, so the cheap-and-consistent trade is free.

Export runs the integrity guard and refuses to write on a blocking failure —
more important than for `select`, since the file outlives the run and carries no
warning of its own. Symbols reaching no journey are omitted, which the skill
reads as "no journeys affected".

Current map: 137 symbols across 21 files (8.4 KB).

### Where the map lives

`--into-target` writes to `<repo>/.testgraph/journey-map.md`, so the map sits in
the repo it describes and is versioned alongside it. The central copy under
`maps/` is **not** automatic — it requires an explicit
`--out maps/<target>.md`, and with no flag at all the map goes to stdout.
Resolution step 3 below is therefore manually maintained. The skill resolves, in order: `$TESTGRAPH_MAP`,
then `<repo-root>/.testgraph/journey-map.md`, then
`~/personal_projects/testgraph/main/maps/<repo-basename>.md`.

### Pinning "the map agrees with the selector" (issues #22, #32)

The consistency claim above needs a test that can *fail*, and two attempts at one
could not. Both recomputed `impacted_closure` from the row's own line range —
which always contains the row's own node, so the closure was seeded with precisely
the set `build_map` had used and equality held by construction. The second attempt
added an `assertEqual` for single-node rows, which made it look stronger while
leaving it just as vacuous.

`MapAgreesWithSelectorTests` derives the seed the way the *other* tool does: a real
git repo whose files match the fixture's node ranges, one commit per map row
editing a line inside that row, and `sel.select` run over `HEAD~1..HEAD`. The
comparison covers journey IDs *and* `verify_manually`, and the fixture is built so
the comparison crosses both axes on which the two tools have actually diverged: a
`.svelte` entry (the #21 disagreement) and a 0.5-confidence edge (a map that
dropped weak paths would under-report). Verified by mutation: narrowing
`_is_product` to `.py`, over-reporting a journey on every row, and dropping
low-confidence journeys each turn it red.

### Warnings live in the artifact (issue #23)

Only *blocking* problems stopped the write. Warnings — "codegraph schema version
unpinned", "N source file(s) newer than the index" — went to the stderr of the run
that produced the map and nowhere else, so the persisted file carried no trace of
them. That contradicted this module's own reason for refusing to write on a corrupt
index: the file outlives the run.

The warning that matters most is freshness, because a stale index makes the map
**under-report** — a symbol missing from it may still reach journeys, which is the
unsafe direction. Warnings now render as a blockquote above the tables, ship in the
`--json` sidecar's `meta`, and have an escalation row in the skill. A clean run
renders no block at all; a warning banner that is always present is one the reader
learns to skip.

### Provenance fails closed (issue #25)

The stamp was `subprocess.run(...).stdout.strip() or "unknown"` — no `check`. A
non-git `--repo`, or any git failure, wrote a map that *looked* stamped while the
consumer's staleness escalation ("`generated from commit` is far behind HEAD") had
nothing to compare and silently never fired. Provenance failing open is the same
defect class as answering `NONE` on an unmappable diff, so it now blocks on the
same path as a corrupt index: no stamp, no map, exit 2, nothing written.

The stamp also reported `HEAD` for a dirty tree, so a map built from uncommitted
code claimed clean provenance — the index can be ahead of the last commit. It is
now `<sha>-dirty`, and both the artifact and the skill say what that means.

**`git -C` walks up.** A plain directory nested inside a repository answers
`rev-parse HEAD` with *that* repository's commit, so the first version stamped a
map with an unrelated project's history — worse than `unknown`, because the
consumer's "far behind HEAD" comparison then runs against a history that keeps
moving and the map reads fresh forever. The target must therefore contain
git-tracked files, and `git status` is scoped with `-- .` so a subdirectory target
is not marked dirty by an unrelated edit elsewhere in a monorepo.

**Deliberate decision — an unborn HEAD blocks.** A freshly `git init`ed target, or
`checkout --orphan`, has no commit to record. Stamping something like
`unborn-dirty` would put a non-comparable string in the field whose only job is to
be comparable to a history, so the export refuses and says which case it is. This
is stricter than issue #25 asked for; the alternative (write the map, stamp it
dirty) is defensible and was rejected on that reasoning, not by accident of
`rev-parse` semantics.

**Failure modes that must not escape:** `git` absent from `PATH` raises
`FileNotFoundError` rather than returning non-zero, which would kill the export
with a traceback and exit 1 instead of the "map NOT written" / exit 2 contract —
`OSError` is wrapped into `StampError`. And a provenance failure prints under its
own `BLOCKED — provenance unverifiable` header, never the index one: the remedy
for a corrupt index is a multi-minute `codegraph index` rebuild, and sending
someone there because `--repo` is not a git repo wastes their time on the wrong fix.

**What counts as dirty:** only paths `select._is_product` accepts.
`impacted_closure` walks *callers*, so a test symbol's closure never contains a
journey entry and no test file has ever produced a row (honeyslate's map has 21
sections, none of them `tests/`); a `.d.ts` has no nodes at all. An uncommitted edit
to either cannot change a single row, so it must not stamp the artifact as
untrustworthy. An untracked `NOTES.md`, testgraph's own `.testgraph/journey-map.md`, or
codegraph's `.codegraph/` must not trip it — honeyslate has two untracked docs
right now, and a marker that is always on is one the reader learns to skip.
Sharing `PRODUCT_EXT` with the selector means #21's widening widened this too,
rather than leaving a Python-only provenance check behind. `git status` runs with
`-uall`: without it git collapses a wholly-untracked directory to `?? web/`, which
hides the extension, and a brand-new `web/App.svelte` read as irrelevant.

### Keyed by symbol, not by line (issue #24)

`skills/testgraph-verify/SKILL.md` matches rows by **symbol name**, treating line
ranges as a hint — they are frozen at the generation commit while the agent's own
edit has already shifted them. Insert 20 lines at the top of `config.py` and
`get_settings` moves from 84–89 to 104–109, so an agent matching by line reads
`load_type_windows`' journeys instead.

The rule was in the skill but the **artifact worked against it**: the table led
with a `lines` column, which is the first thing an agent looks at, and said
nothing about the ranges being stale. Columns are now
`symbol | journeys | lines (at generation — stale hint)`, the caveat is in the
preamble *and* the column header (an agent may jump straight to one `###`
section), and `render_markdown` — previously the only wholly untested part of the
pipeline, asserted nowhere beyond "the file exists" — now has `MarkdownRenderingTests`
covering column order, per-row cell contents, the `!` safety marker, and the
commit stamp the staleness escalation keys off.

### The consumer's escalation contract

Crucially, the map only lists symbols that existed when it was generated, so the
skill escalates to `testgraph.select` rather than concluding, for: an added symbol
or file, a deletion or rename (where `select` reports unbounded impact the map
cannot express), a file with no section at all, and a stale commit stamp. A symbol
counts as "no journeys" **only** when the map covers its file and lists other
symbols from it; every other absence is *unknown*. Reporting "none" for an unknown
is the most harmful thing the skill can do, and an earlier draft instructed
exactly that.

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

Each verdict records the `base_sha` it was adjudicated against. An excuse is a
claim about a call path at a point in time: if the code moves, the path may now
exist and a stale excuse would suppress a real miss — this project's own failure
mode, reintroduced inside its harness. Mismatches print as `STALE EXCUSES` and
fail under `--strict-adjudications`.

Current result: **20 sites, adjudicated recall 1.00, mean worst rank 3.17, mean
3.33 of 8 journeys named.** Three disagreements, all adjudicated as oracle false
positives with sole-caller evidence.

## Registry Rot (issue #19)

`registry.unresolved()` reports journeys where **no** entry symbol resolves to a
node. Such a journey can never be selected: it silently disappears from every
answer while the registry — and the map's legend — still advertise it as covered.
Rename a FastAPI handler without updating `journeys/honeyslate.json` and
testgraph would report that no change can affect that journey.

`select` and `export` both **block** on it. A journey with at least one live
entry is not flagged — one resolvable entry is enough to keep it selectable.

`select(..., strict_registry=False)` downgrades it to a reported
`unresolved_journeys` field, for one specific case: the accuracy harness checks
out **historical** commits, where a journey that did not exist yet is expected
rather than rot. The harness passes the flag and drops absent journeys from that
commit's oracle, so they count against neither recall nor precision. Without the
distinction, blocking silently shrank the scored set and moved mean precision
from 0.84 to 0.69 — a headline metric changing for a reason unrelated to
selection quality.

## Live Entry Drift (issue #7)

`registry.unresolved()` compares the registry to the **index**. Both can agree and
both be stale, because the index is a snapshot: rename a handler, run `select`
before `codegraph index`, and the old node still resolves, the journey is still
selected, and every answer is about a symbol that no longer exists. Nothing else in
the pipeline reads the working tree, so nothing else can catch it.

`registry.live_drift(repo, registry)` parses the source with Python's `ast` and
reports entries the index resolves but the file does not define. It is surfaced as
a warning plus an `entry_drift` field in `select --json`, and — via #23's warning
block — inside the exported map, which needs it more because it persists.

**Deviation from the issue, deliberate.** #7 specified RunEcho for the live parse.
testgraph is a CLI with no MCP client, and stdlib `ast` answers the same question
for the only language any journey has entries in today. Non-Python entries are
reported as `unchecked` rather than silently passing, so the gap is visible instead
of assumed away. If a frontend journey is ever registered, that row starts showing
up as unchecked and asks for a real parser.

**Two channels, not one.** Entries no parser covers (`.svelte`, `.vue`) are
returned by `unchecked_entries()`, never as drift. Emitting them as drift put "the
index was not fully trustworthy" in every exported map the moment a frontend journey
existed — and prescribed `codegraph index`, which can never clear an unverifiable
entry. A permanent banner with an impossible remedy is the failure
`_map_relevant`'s own docstring warns about.

**The remedy depends on the reason.** `live_drift` reads the working tree while the
rest of `select` reads committed history, so a hard-coded "run `codegraph index`"
sent an agent to spend minutes rebuilding an index that cannot change the answer for
the commonest trigger — an uncommitted rename. `REMEDIES` maps each reason to what
actually fixes it: re-index, commit first, fix the registry, or fix the syntax error.

**Reported, never blocking.** The check approximates in one direction: an entry
re-exported into its registry `file` rather than defined there would be a false
positive, so imports count as definitions and the result is a warning. Blocking
would break real runs to report a freshness problem whose remedy is `codegraph
index`.

**Registry `file` values are suffixes.** `resolve_symbol` matches them with a LIKE,
so `routers/tasks.py` means `backend/app/routers/tasks.py`. The first version joined
them onto the repo root and reported all 16 of honeyslate's entries as "file is
gone" — caught by running it against the real registry rather than only the fixture,
and pinned by `test_registry_file_is_a_suffix_not_a_repo_relative_path`. Cost on
honeyslate: 87 ms, zero drift rows.

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

## Non-Python Selection (issue #21)

`PRODUCT_EXT` covers `.py/.js/.jsx/.mjs/.cjs/.ts/.tsx/.mts/.cts/.svelte/.vue`;
`.d.ts`-family declarations are excluded (no runtime behavior, no nodes).
Previously only `.py` was seeded, so a frontend change produced zero seeds and
`select` answered `journeys to test: NONE` — while `export`'s map, walking all
indexed nodes, listed those same files against J8. Two tools, different answers,
and the map was right.

Widening is not free. The original claim that it "cannot invent impact" holds only
for extensions the indexer does not cover; where it *does* cover them, a false
positive can arrive over a weak edge, and one did (see Known Limitations, 0.84 →
0.68). `_is_test` gained the JS/TS conventions (`.test.`, `.spec.`, `__tests__/`)
and now matches test directories as whole path segments, so a repo-root `tests/`
or `__tests__/` is excluded rather than seeded as product code (issue #33).

## Zero-Seed Changes Degrade, Never Answer NONE (issue #29)

Widening the extension set created a second way to produce confident silence: a
file `PRODUCT_EXT` accepts whose changed lines resolve to **no** node — a newly
added module the index predates, or an extension the index does not actually
cover in this repo. Seeds stayed empty and the answer was
`journeys to test: NONE` with `recall_degraded: false` and no warning, which is
the exact failure class #21 exists to kill.

Each changed file's node set is now computed on its own; an empty one joins the
same `unmapped` channel the whole-file path uses, so it warns and degrades to
every journey with `verify_manually: true`. Per-file sets matter: with one running
total, a mapped file in the same commit would mask the unmapped one.

Measured effect on the labeled set: none. Recall 1.00 and mean precision 0.68 are
byte-identical with the degrade reverted, because no labeled commit exercises the
zero-seed path (`0b4135f` already degraded via a whole-file change). The guard is
justified by the reproduced failure in #29, not by a metric move.

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

## Who runs the journeys (decision, issue #8)

Two holes in the parent plan reduced to one question, and it blocked the results
ledger (#10) from having a writer: something must actually *run* the journeys, and
something must reset the environment between runs. Three options were on the table —
(a) testgraph stays a pure selector and requires a runnable environment as input,
(b) testgraph owns a seeded environment for honeyslate, (c) execution happens in the
agent loop.

**Decided: (c) with a homelab reset. `/autorun` runs the journeys; Proxmox snapshots
reset them.** Consequences worth stating, because they are what the decision buys
and costs:

- **testgraph does not own an environment.** No seeded-fixture code, no docker
  compose, no test-data loader lands in this repo. Seeded, resettable E2E
  environments are usually the most expensive part of E2E, and this repo stays the
  selector rather than absorbing that cost.
- **`/autorun` is the ledger's writer.** #10 was dead schema without one. The
  contract testgraph owes it is already built: `select --json` says what to run.
  What is still missing is the inbound half — a `testgraph record` that accepts
  results — which is #10's actual scope.
- **Reset is infrastructure, not application code.** Proxmox snapshot rollback is a
  homelab capability that exists independently of this project, so the reset story
  costs nothing to build here and is not testgraph's to maintain.
- **The risk accepted:** journeys run only as often as `/autorun` runs, so the
  ledger accumulates on that cadence rather than per-commit. That is the trade for
  not building an environment; if the ledger turns out to need denser data, this
  decision is the thing to revisit first.

## Known Limitations

- **Scope:** honeyslate only. Other repos need their own journey registry. Frontend
  files are now *seeded* (issue #21), but no journey has a frontend entry point, so
  a frontend change is only visible where it reaches a backend entry.
- **Precision on shared symbols:** a config/model edit fans out to most journeys
  by design (recall-first). Mean precision 0.68 on the labeled set; the low case
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
- **Precision is 0.68 with non-Python selection on, down from 0.84.** Widening
  past `.py` (issue #21) added J8 as a false positive on two labeled commits,
  both via frontend files. Recall stays 1.00. Crucially the additions arrive at
  `confidence 0.5, verify_manually: true` while the genuine journeys are 1.0 —
  B1's flag firing on real data for the first time, exactly as predicted when it
  shipped. The labeled oracles were authored under a Python-only selector and may
  need re-labelling before 0.68 is read as a regression.
