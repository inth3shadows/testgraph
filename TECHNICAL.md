# Technical Reference: testgraph

Phase-1 spike. This documents the built selector; the broader roadmap (ledger,
trace discovery) lives in the private plan
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
- `testgraph/propose.py` — drafts a registry for a repo that has none: ast scan
  for route-decorated handlers, index resolution, spot-check derivation, declared
  blind spots. Writes `approved: false`.
- `testgraph/ledger.py` — the append-only `selection`/`outcome` store and the
  `caught`/`missed`/`unasked`/`unbaselined` join over it. Never raises; a torn
  line is skipped rather than fatal.
- `testgraph/record.py` — the `outcome` writer and the `--summary` /
  `--export-kb` readers. Refuses a journey id the registry does not know.
- `journeys/honeyslate.json` — 8 journeys (submit, browse, edit, reschedule,
  comments, auth, gcal sync, scheduler) + `spot_checks` floors for the guard.
- `harness/plugin/tgtrace.py` — pytest plugin, loaded into the TARGET's
  interpreter; records every function entered during each test body. Imports
  nothing from this project, and sits in its own directory so only that
  directory joins the target's `PYTHONPATH` — exporting `harness/` would shadow
  the stdlib `trace` module for the traced suite.
- `harness/trace.py` — runs a target's suite under that plugin and writes the
  trace JSON.
- `harness/ground_truth.py` — joins traces to journeys and scores the static
  footprint against them: `traced_only` / `static_only` / `unresolved`.
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
- `--registry` resolves from `--repo` when omitted: the `journeys/*.json` whose
  `target` matches the repo name, seeing through the bare-worktree layout
  (`…/signedintake/main` → `signedintake`). It **refuses** when nothing matches.
  This default used to be honeyslate's registry, hardcoded, so any other `--repo`
  silently loaded the wrong journeys and then reported the resulting disagreement
  as `registry is stale against the index` — a wrong diagnosis of a wrong input.
  The integrity guard did block rather than answer, which is why it surfaced at
  all; matching on `target` removes the trap instead of relying on the guard.
- `git config testgraph.enabled false` disables the pre-push hook for one repo.
- `TESTGRAPH_STATE_DIR` relocates the invocation log (default
  `${XDG_DATA_HOME:-~/.local/share}/testgraph`).
- Registry `spot_checks`: `{symbol: {min_caller_edges, file}}` — the guard floors.

## Deployment

No service to deploy — a CLI run against a target repo's index. The **live**
consumer is `hooks/pre-push`, installed by `hooks/install.sh` into the shared
hooks dir (`git rev-parse --git-common-dir`/hooks) of every repo with an approved
registry, so one install covers all of that repo's worktrees. It runs
`python3 -m testgraph.hook`, which renders a push-sized summary and appends a
`selection` row to `~/.local/share/testgraph/ledger.jsonl`.

Two properties are load-bearing and neither is about selection quality:

- **It cannot fail a push.** Every path returns 0 — blocked guard, missing index,
  missing registry, traceback, `timeout 20`. A hook that can block gets
  uninstalled, and an uninstalled hook is the zero-invocation state again.
- **It prints short.** `select._render` emits one `NOTE` per unparseable entry
  symbol — fourteen on signedintake, identically, on every push, and no re-index
  can ever clear them. The hook drops those, caps the journey list at 8 and the
  warnings at 3, and says how many it hid. A reader who learns to skip the block
  has learned to skip the answer.

The installer **refuses** a repo whose `pre-push` already belongs to another tool
instead of appending to it. Appending was the first design and review killed it
twice over: a hook ending in `exit`/`exec` — the ordinary way to write one — leaves
the appended block dead forever while the installer reports success; and a hook that
ran `set -e` leaves it set for the appended block, where one unset `git config
wt.base` exits non-zero and *fails the push*. Cohabiting properly means replaying
stdin between blocks, which is real complexity for a case that does not exist here:
no repo on this machine has a `pre-push` hook. The hook body still guards its
assignments with `|| true` in case someone merges it by hand.

`skills/testgraph-verify` (reads the exported map) and a CI gate reading `--json`
remain the other two intended consumers.

**An MCP wrapper was listed here as a future consumer and has been dropped.**
Measured 2026-07-30 across every session on this machine: 82 tool calls touched an
exported map, **none of them a whole-file `Read`** — all were `grep`/`cat`, median
275 B (~68 tokens), p90 ~410 tokens. A server returning the same rows would save
nothing worth a process. The probe that killed it, including the pre-registered
decision rule, is `~/.claude/plans/testgraph-mcp-vs-map-probe.md`.

The same probe found the thing that actually matters: those 82 calls were all
testgraph's own development, and `Skill(testgraph-verify)` had been invoked **zero
times** (issue #49). The pre-push hook exists to end that, and it carries its own
scoreboard: the `selections` count in `testgraph.record --summary`. If that number
is still near zero a week after install, the hook failed too — and the honest
conclusion would be that nothing in this workflow wants a selector, not that the
next consumer will be the one.

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
rendered as a footnote in the map and a `NOTE:` line in `select`'s human output, and
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

## Proposed Registries (issue #6)

Hand-authoring `journeys/<target>.json` is the reason testgraph covers one repo,
and "registry completeness is manual" is the documented silent-under-selection
risk. `testgraph.propose` converts authoring cost into review cost.

**The split: this module does discovery, an agent does judgment.** Discovery —
which symbols are HTTP entry points, and do they resolve in the index — is
deterministic and belongs in `ast` plus the graph. Grouping and naming are
judgment a human has to approve anyway, so they live in
`skills/testgraph-propose/`. No API key and no runtime dependency were added: the
same reasoning that made `live_drift` parse with `ast` instead of calling RunEcho
("testgraph is a CLI with no MCP client") applies unchanged here. The draft is a
complete, valid registry on its own, so the tool degrades rather than fails when
no agent is involved.

**Two scanners, one pipeline (issue #46).** `scan` reads Python decorators;
`scan_typescript` reads Next.js conventions. Both return the same
`(path, symbol, routes)` tuple, so id assignment, index resolution and journey
building are language-agnostic and neither scanner can special-case itself. The
TypeScript side is regex over lines, not a parser: no stdlib TS parser exists and
a dependency was rejected on the same grounds as the API key. The safety net is
that every candidate must resolve to an index node, so a sloppy scanner degrades
to *omission* — visible in the found count — rather than to a bad entry.

Three Next.js shapes, calibrated against signedintake rather than guessed:
route-handler exports in a `route.ts`, exported functions in a module-level
`'use server'` file, and the default export of a `page.tsx`. The URL comes from
the **file path** (strip through `app/`, drop the role leaf, drop `(group)`
segments, keep `[param]`) — a better signal than the Python case, where it has to
be read out of a decorator argument.

`'use server'` must be the module's **first meaningful line**.
`src/app/login/page.tsx:23` carries an indented one inside a component body — an
inline action — and accepting the directive anywhere in the file would register
every export of that component as a journey entry.

Journey ids use the **parent directory**, not the file stem, when the stem is a
Next.js role name (`route`, `page`, `actions`, `layout`, `index`). Every handler
in the convention lives in a `route.ts`, so stem-based ids collide repo-wide and
`assign_ids` widens all of them to paths like
`J_src_app_api_payment_requests__requestId__status_route_GET`.

**Measured against signedintake:** 19 entries drafted, recovering **all 12** the
hand-authored registry uses (`submitForm`, `issueFormLink`, `POST`, both `GET`s,
`validateAndPreview`, `createFormAndLink`, `createPaymentUpdateRequest`,
`simulatePaymentCompletion`, `Page`, `StaffIndexPage`, `SubmissionDetailPage`,
`FormDetailPage`) plus seven it does not — a registry testgraph's author did not
write. honeyslate (17), llm_history_audit (30) and coriolis-local (207) are
unchanged.

**Discovery** matches decorators by *attribute* (`.get`, `.post`, `.route`,
`.websocket`, …) rather than by receiver name, which makes it work for FastAPI,
APIRouter, Flask, Blueprint, Starlette, Sanic and AIOHTTP without naming any of
them. Module-level `def`s only: a function-local handler is not importable, so it
can never be an entry symbol the registry could resolve.

**Split, never merge.** The draft is one journey per handler. Splitting is the
safe direction — more journeys, each narrower, recall unaffected — while merging
two flows behind one id hides which of them broke. So the mechanical pass never
guesses a boundary.

**Unresolvable candidates are excluded, not shipped.** `registry.unresolved`
treats a journey with no resolvable entry as **blocking**, so one bad drafted
entry would take the whole registry down instead of degrading. They are reported
in `unresolved_candidates` with the reason.

**Blind spots are declared, not guessed.** Schedulers, CLI entry points,
callback-registered consumers and middleware carry no decorator. honeyslate's J8
(`scheduler.sweep`, `scheduler.start`) is this class — a human knew to add it. A
heuristic that tried to infer them would invent journeys, so the draft names the
gap and the skill sends the agent to close it.

**Approval is a first-class field.** Drafts carry `"approved": false`;
`select` and `export` emit `UNAPPROVED REGISTRY` on the shared warning channel and
still run, the same posture as `RECALL DEGRADED`. Blocking was rejected — it would
make the proposer useless until a human edits JSON. A **missing** `approved` key
warns too: an unmarked registry is unknown provenance, not an approved one. That is
safe from the always-on-warning failure `unchecked_entries` documents, because the
named remedy always clears it.

**Measured against honeyslate:** 17 handlers found, covering all 14 route-handler
entries the hand registry has, and surfacing three it misses — `delete_task`,
`google_status`, `google_selftest`. J8's two scheduler methods are the declared
blind spot. Two defects were caught by running it on the real repo rather than the
fixture, both now pinned by tests: the spot-check picked `login` out of
`backend/tests/conftest.py` (a test fixture anchoring the product-graph guard), and
the non-Python blind-spot count read 96 because `.svelte-kit/` and `build/` were
not in `_SKIP_DIRS` — 13 is the real number.

### Spot-check pins must be stable, not merely popular (issue #43)

Ranking candidates by raw fan-in picked `Button` (232 edges,
`frontend/src/components/ui/button.tsx`) on coriolis-local and `get`
(`frontend/src/api/client.ts`) on llm_history_audit. The spot-check is the
*blocking* half of the guard and its printed remedy is `codegraph index`, so a
pin whose count drifts under normal work blocks every run with a fix that cannot
clear it.

Two properties matter and fan-in only supplies one:

- **Sensitivity** — a high count with a tight floor detects a small edge loss.
- **Stability** — the count must not drift through ordinary work.

Stability is measured as **mean commit-count of the CALLER files** over the last
500 commits. Not the definition's own file: the floor breaks when call sites are
deleted, and `button.tsx` is a stable file whose callers churn constantly, so
definition-file churn would rank it the safest pin in the repo.

**Ranking on churn ascending was tried first and is wrong.** It makes fan-in a
pure tie-break, and since some symbol always has near-zero churn the pick
collapses to the quietest one: honeyslate went from `get_settings` (15 of 19
edges) to `hash_token` (2 of 3) — an obscure symbol in place of a real canary.
The score is `fan_in / (1 + caller_churn)`: quiet *and* load-bearing.

**`MIN_TOLERANCE_EDGES` turned out to matter more than churn.** At
`SPOT_CHECK_FLOOR = 0.8` a 3-edge symbol has a floor of 2, so deleting one caller
blocks every run — small fan-in is the most fragile pin there is, whatever its
churn. Requiring `count - floor >= 2` implies a minimum fan-in of 10 and removes
that class outright. It also corrects the premise #43 was filed on: `Button`'s
232 edges leave a 47-edge tolerance band, which is the *widest* in that repo, so
the original "deleting a few `<Button>` usages blocks the guard" was wrong by an
order of magnitude. The fragile pins were the small ones.

With both rules, honeyslate's automatic pick is `now` and **`get_settings`** —
the latter being the symbol a human independently chose for the hand-authored
registry.

No git history (shallow clone, fresh repo, plain directory) scores every
candidate at 0 and the order collapses to fan-in, i.e. the pre-#43 behaviour.
The draft records which mode ran in `spot_check_basis`, and ships
`spot_check_candidates` so a reviewer can swap a pin without re-deriving one.

**Fan-in does not rank entry points.** The draft records it, but a handler is
called by the framework, not by the codebase, so its inbound-edge count is
structurally ~0: all 17 honeyslate candidates scored 0. It is omitted from the
human output rather than printed as a column of zeros. This is also why
`_spot_checks` excludes entry symbols — pinning one sets a floor of 0 and the
guard would pass on a wrecked index.

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

## The results ledger (issue #10)

`testgraph/ledger.py` + `testgraph/record.py`. One append-only JSONL at
`~/.local/share/testgraph/ledger.jsonl`, two row kinds discriminated by `kind`:

| kind | written by | says |
|---|---|---|
| `selection` | `testgraph.hook` (the pre-push consumer) | testgraph named these journeys for this commit |
| `outcome` | `testgraph.record` (a human, or `/autorun` per the #8 decision) | this journey was run at this commit and passed/failed |

Neither kind is worth much alone — a selection log answers "was the tool called",
an outcome log answers "did the app break". The value is the join on
`(repo, commit)`, which yields the number this project has so far *asserted*
rather than measured: a journey that failed on a commit whose selection did not
name it.

### Four buckets for a failure, and why they must stay apart

- **caught** — the selection **answered** for that push, named the journey, and
  the journey was **known-good at the push's base**.
- **missed** — the selection **answered** for that push, did not name the
  journey, and the journey was **known-good at the push's base**. A real silent
  under-selection, the one failure mode a recall-first selector must not have.
- **unasked** — no selection answered for that commit. testgraph was never asked,
  so this says nothing about it.
- **unbaselined** — a selection answered, but nothing records the journey passing
  at that push's base, so the breakage may predate the push.

`observed_recall` is `caught / (caught + missed)` and is `None` — not `0.0` —
until something is actually judged.

The last two buckets are each a bug that was caught in review, and both had the
same shape: a row that says nothing about the selector being counted as evidence
against it.

**`unasked` is not just "no row".** `hook.run` writes selection rows on four
*non-answer* paths — `NO_REGISTRY`, `NO_INDEX`, `ERROR`, and `BLOCKED` — each with
no `journey_ids`. Treating those as "asked, and it named nothing" meant a tripped
integrity guard scored every later failure at that commit as a silent miss. Worse,
it chained: one JSON typo in an approved registry makes `resolve_for_repo` return
`None` (it swallows `ValueError`), so every push logs `NO_REGISTRY` and every
subsequent failure is blamed on the selector. Only `status == "OK"` counts as an
answer.

**`unbaselined` exists because the join compares a delta against a state.** A
`selection` row answers "what could `base..head` break" — a delta. An `outcome`
row asserts "journey J is broken *at* this commit" — a state. Joining on the head
commit alone scores this as a miss:

> push A..B breaks J3, and the selection for B correctly **names** J3. Nobody runs
> journeys. Push B..C touches only `README.md`, so the selection for C names
> nothing. The developer runs J3 at `HEAD` (=C) — exactly what USAGE.md says to do
> — and it fails, so it is recorded at C.

The selector was right both times, and the old join reported a silent miss and
dropped `observed_recall`. Requiring a recorded `pass` at the push's base before
blaming the selector removes that: **you only get to call it a miss when you had a
green baseline to regress from.** The cost is that misses stay rare unless
journeys run on every push — which is the discipline this number needs in order
to mean anything, so the cost is the point.

**The baseline gates `caught` and `missed` identically, and that symmetry was
missing for one release.** As first shipped, only `missed` required the green
baseline; `caught` required nothing. So the paragraph above was implemented in
one direction and the bucket list said "the selection for that push named the
journey" with no baseline clause — which is the *same* defect as the two above
it, pointed the other way: a row that says nothing about the selector counted as
evidence, here **for** it.

A journey that was never green banked a `caught` on every push whose selection
happened to name it. That is a perfect score for zero information — the journey
was already red, so naming it predicted nothing — and it was one-directional:
pre-existing breakage could only ever *raise* `observed_recall`, never lower it,
because the same history with a selector that named nothing fell into
`unbaselined` and was excluded from scoring. Two pushes of an always-red journey
read **2 caught / 0 missed / recall 1.00** one way and **0 / 0 / `None`** the
other.

It also propagated into the ranking gate. `RankingGateTest` built twenty pushes
of an always-red journey with no baseline anywhere and asserted the gate opened;
its `_push` helper's docstring claimed it recorded a green baseline and never
did. Twenty `unbaselined` failures now correctly leave the gate shut, and
`test_always_red_history_cannot_open_the_gate` pins that.

You only get to **credit** the selector when there was something to regress
from, for the same reason you only get to blame it then.

**Two things the widened `unbaselined` bucket then collided with, both found in
review of the fix itself.**

*The density counter and the score had diverged.* `ready_for_ranking` is
`judged_commits >= 20 AND judged > 0`, but `judged_commits` counted any commit
carrying a selection plus a pass/fail — including the `unbaselined` failures
`judged` now excludes. Twenty-four always-red pushes plus **one** properly
baselined catch read as `judged_commits: 25, caught: 1, ready: True,
observed_recall: 1.00`: the gate opened and handed ranking a recall drawn from a
single observation. A commit now counts toward density only if it carries a
`pass` a selection answered for, or a failure that was actually **scored**.

*`base` was never resolved.* `hook.run` normalises `head` into `commit` through
`resolve_commit` — with a comment explaining that two spellings of one commit
join to nothing — and stored `base` **verbatim** on the line above. That was
harmless until the baseline started gating the score: the lookup joins against
outcome rows keyed on resolved shas, so `--base HEAD~1` matched nothing, every
failure on that push fell to `unbaselined`, and `observed_recall` was pinned at
`None` for any caller passing a symbolic base. `base_commit` now carries the
resolved form; `base` is kept as spelled, because that is what the rendered
output should show.

The pattern in both: a change to what a bucket MEANS has to be chased into every
counter and every sentence that reads it. Neither of these was in the diff that
introduced the rule.

Two smaller rules follow from the same principle. Observations are deduplicated
on `(commit, journey)`, last verdict wins, so re-recording one failure does not
multiply the headline count. And the commit key must be a 40-hex sha:
`resolve_commit` returns `None` rather than the rev verbatim when git cannot
answer, because `record --repo ~/personal_projects/honeyslate` (the bare-worktree
*parent* — a directory whose `repo_name` still resolves, so every other check
passes) filed every outcome under the literal key `"HEAD"`, where two unrelated
commits' failures joined to each other and produced one fabricated catch and one
fabricated miss.

### Storage: why local JSONL, and not the KB as #10 decided

Issue #10 decided the ledger belongs in the shared `kb.*` Postgres rather than a
local store, for three reasons. One was already false, and the decision as a whole
is unbuildable at the write path:

- **"lost to `wtclean`"** — false. `state_dir()` resolves to
  `~/.local/share/testgraph`, outside every worktree. That was already true of
  `invocations.jsonl`.
- **"invisible from the work Mac"** and **"no reviewer or browsable surface"** —
  both stand, and both are real requirements.
- The blocker: **the KB is reachable only through an MCP server that an agent
  drives.** There is no Python client, so `hook.py` cannot be the writer without
  taking on a tunnel, a credential and a network round-trip inside a hook whose
  entire contract is that it never fails a push. A KB-only ledger would have *no
  writer at all* — which is precisely the defect #8 named ("the ledger has no
  writer and would ship as dead schema"), relocated one layer up.

Resolved as a split rather than a reversal: the local JSONL is the write path, and
`record --summary --export-kb` emits a proposal payload an agent pushes into the
KB. The export deliberately does **not** name a target table — KB conventions
require `kb.read.search` first and `kb.propose.extend` over a new table, which is a
decision that needs the KB in front of it, not a hardcoded guess in a CLI that has
never read it.

### Ranking is not wired to this yet, on purpose

#10 asks for staleness and failure history to feed the next ranking. It should —
but at the time this shipped the ledger held **zero rows**: the pre-push hook had
not fired once since it merged. Ranking on an empty history is manufacturing a
signal. `summarize()` therefore exposes `ready_for_ranking`, gated at
`MIN_JUDGED_COMMITS = 20` — the size of the seeded-regression eval (#5), which is
the smallest set that has yet said anything falsifiable about this selector, and a
ranking signal is a weaker instrument than that eval rather than a stronger one.
Below the threshold, `--summary` prints the distance to it.

The gate needs **both** enough judged commits and at least one judged *failure*,
and `skip` verdicts count toward neither. Twenty commits' worth of "deliberately
did not run this" used to flip the flag while carrying zero failure evidence —
which is the only quantity the threshold is actually reasoning about.

`invocations.jsonl` is still read if present, as `selection` rows, so no install
loses its history to the rename.

## Second registry: signedintake (issue #11)

`journeys/signedintake.json` + `maps/signedintake.md` — a Next.js / TypeScript app
with **zero Python**: claimant submits a signed form, staff issues a link, YAML
onboarding, submission review, payment-update request, Stripe webhook settlement,
status polling, form detail. (This was 8 hand-authored journeys; it is now 13 —
see *Re-derived from `propose`* below. The measurements in this section were taken
against the 8.)

**What this proves — the machinery is not honeyslate-shaped.** Entry resolution by
name+file suffix, the reverse closure, `build_map`, the integrity guard with its
caller-edge floors, and the schema pin all work unchanged against a `.ts`/`.tsx`
index: 169 symbols across 38 files, `unresolved()` empty, guard passing. Two features
built this week fired on real data the first time they met a second target:

- **The #23 warning block.** The map carries "81 source file(s) newer than the index"
  at the top, because signedintake's index is from 2026-07-17 and the repo moved on.
  A reader of that file cannot miss that it under-reports.
- **The #7 unchecked-entries footnote.** Every entry is `.ts`/`.tsx`, so the live
  parse cannot verify any of them and the map says so once, as a limitation — not as
  eight integrity alarms with a `codegraph index` remedy that could never clear them.

**Selectivity, measured after rebuilding the index.** The first pass over the 14
commits up to `0305ada` returned all 8 journeys on 8 of them, every one
`recall_degraded: true` — and that was diagnosed as index coverage rather than code
coupling, because `src/lib/seal-submission.ts`, `src/lib/audit.ts`,
`src/lib/labels.ts` and `staff/[submissionId]/pdf-actions.ts` had zero nodes.
`codegraph index` was then rerun: **98 files, 662 nodes** (was 78 files, 547), those
four files now carry 14/5/2/11 symbols. Re-running the same sweep:

| | signedintake (14 commits) | honeyslate (5 labeled commits) |
|---|---|---|
| mean selected | **2.21 / 8 (27.7%)** | 3.4 / 8 (42.5%) |
| journey-runs avoided | **72.3%** | 57.5% |
| `<= 2` journeys | **12 of 14** (4 of them zero) | 3 of 5 |
| `3-5` journeys | **0 of 14** | 0 of 5 |
| `>= 6` journeys | **2 of 14** | 2 of 5 |
| degraded runs | **0** | 0 |

**The diagnosis held:** zero degrades after the rebuild, so all eight of those 8/8
answers were the incomplete index, not the codebase. The earlier numbers were a tool
artifact, as flagged at the time.

**The bimodal shape replicated on an independent codebase.** Narrow or total, nothing
between: 12 commits touch 0-2 journeys, 2 touch all 8 (a merge spanning 8 files, and a
feature editing `db/schema.ts` — a shared symbol, which fans out by design). This is
#13's falsification test, and the answer is that the shape is a property of how change
lands in a codebase, not of honeyslate. Selectivity was *better* here than on
honeyslate (72.3% vs 57.5% avoided).

**What is still untested:** #13's criterion said 20+ journeys. This is a second
8-journey registry, so the shape replicates at the same scale — whether the broad tail
grows or shrinks as journey count rises is still open, and #10's ledger is what would
answer it from real runs rather than a 14-commit sweep.

**Methodology limit, stated:** these numbers come from ONE index built at `0305ada`,
not a per-commit index like `harness/accuracy.py` builds for honeyslate. For commits
at or before the index commit that is sound for coverage but not for line alignment —
a hunk's line numbers are read against the indexed snapshot, so an older commit's
ranges can map to a neighbouring symbol. There is also no hand-labeled oracle here, so
these are selection *sizes*, not precision or recall. A per-commit harness for
signedintake would fix the first and is the honest next step if these numbers ever
need to carry weight beyond "the shape replicates".

### Re-measured at 14 journeys, per-commit (`harness/selectivity.py`, 2026-07-31)

Everything above was measured against the **8-journey** registry that `17da9e1`
retired. The pre-push hook queries the 14-journey one, so the published numbers
described a registry nothing was using. `python3 harness/selectivity.py` re-runs the
sweep — same 14 commits through `0305ada` — with the methodology limit above closed:
each commit is checked out into its own worktree and indexed fresh, so line spans and
`files.content_hash` agree with the tree by construction. That is no longer optional
bookkeeping: after the content-drift guard, a shared tip-commit index would disagree
with almost every historical commit's bytes and the sweep would measure the guard
firing, not selectivity.

| | 14 journeys (per-commit index) | 8 journeys (shared index) |
|---|---|---|
| mean selected | **2.64 / 14 (18.9%)** | 2.21 / 8 (27.7%) |
| journey-runs avoided | **81.1%** | 72.3% |
| `<= 2` journeys | **11 of 14** (4 of them zero) | 12 of 14 (4 zero) |
| selects every journey | **0 of 14** | 2 of 14 |
| degraded runs | **0** | 0 |

Histogram, journeys selected -> commits: `{0: 4, 1: 2, 2: 5, 3: 1, 11: 2}`.

**"Narrow or total" survived; "total" stopped being literal.** The gap in the middle
is still there — nothing lands between 3 and 11 — but the ceiling fell from 8/8 to
**11/14**, reached by two related PDF-regeneration commits. The two commits that used
to select *everything* now select 79% of everything. J13 (`instrumentation.register`)
and J14 (the seed-staff CLI) are structurally isolated and are never pulled in by the
shared-symbol fan-out that produced a literal 8/8.

That is a partial answer to the open question above — whether the broad tail grows or
shrinks as journey count rises. Here it shrank, and selectivity *improved* with a
larger registry (81.1% vs 72.3% avoided), because the journeys added were not on the
shared-symbol paths. One repo at one step in registry size is not the 20+ journey
falsification #13 asked for, and the mechanism named — isolated journeys dilute the
fan-out — predicts the effect reverses for a registry grown along shared paths
instead. Still selection *sizes*, still no hand-labeled oracle here.

### Re-derived from `propose`, then reviewed (2026-07-30)

The 8 journeys above were hand-authored. Once `testgraph.propose` learned Next.js
(#46) it was run against the same repo as its first real use, and the draft was
reviewed into the registry that now ships: **14 journeys over 23 entries**, map
regenerated at 190 symbols across 57 files.

The draft found 19 handlers: all 12 hand-authored entries, plus **7** the hand
registry did not have. Grouping kept the original ids and merged two of the seven into
existing journeys — `OnboardingPage` into J3, `FormsIndexPage` into J8 (renamed *staff
browses forms*, index and detail being one flow). The other five became new journeys:
sign-in (J9), PDF regenerate-and-download (J10), the customer's dev-provider payment
page (J11), and the marketing home page (J12).

**Two further journeys came from the blind-spot table, not from the scan** — which is
the part of the skill that earns its keep:

- **J13**, `instrumentation.register()`: a Next.js lifespan hook that fails closed on
  production misconfiguration. A change to it can stop the app booting.
- **J14**, `npx tsx scripts/seed-staff.ts`: a CLI entry point. The first review of this
  repo checked `package.json` `scripts` — which hold only `next`/`eslint`/`vitest` —
  and wrongly concluded there were none. `scripts/` is where they actually live, and
  `src/lib/seed.ts` appeared in no row of the map until J14 registered it.

`simulatePaymentCompletion` is deliberately an entry of **both** J5 and J11: staff
create the payment request, the customer completes it, and that symbol is the hinge.

**Remaining blind spots checked and empty here:** no `middleware.ts`, no `pages/`
router, no anonymous default exports, no non-Next.js router, no schedulers. Knowingly
uncovered: client components reached by in-browser navigation (`SimulateButton`,
`PasskeyButton`, `RegeneratePdfButton`), each registered indirectly through the page or
action it calls; and `scripts/pg-backup.sh`, a shell entrypoint with no symbols in the
index to register.

The registry carries `"approved": true` and a `note` recording that review, so the
provenance warning added in #42 no longer fires for it.

### Two defects fell out of shipping it

**A shared entry symbol collapsed to one journey.** `resolve_entries` built
`node_id -> journey_id`, so a symbol listed as an entry of two journeys kept only
whichever journey came last in registry order. `simulatePaymentCompletion` is the first
registry entry to exercise it in either target, and the regenerated map caught it: the
row read `J11` alone while the legend still advertised the symbol under J5. The
duplication that was supposed to protect recall for both flows had *halved* it for one.

`unresolved()` cannot see this — it re-resolves each entry rather than reading the map,
so a journey whose entries were all shared with a later journey would vanish from every
answer while the legend still listed it. That is the #19 failure mode arriving through
a door the #19 guard does not watch. `resolve_entries` now returns `node_id -> {jid}`
and `build_map` and `select` fan out over the set.

**Journey ids sorted as plain strings.** Passing nine journeys made the exported map
read as shuffled — `J10`-`J14` sorted between `J1` and `J2`. `registry.journey_sort_key`
splits an id into prefix and number so digits compare numerically; every
journey-ordered surface uses it — the map legend, the per-symbol rows, `select`'s rank
tie-break and its unbounded-impact fallback, and the `unresolved` / `live_drift` /
`unchecked_entries` loops, so inserting a `J15` out of order stays readable.

Both were invisible at 8 single-digit journeys with no shared entries, which is why no
fixture caught either — the same lesson as #43 and #44: the bug arrives by *running*
the thing on real data at a scale the fixtures never reach.

## The null hypothesis: what selection is worth at 8 journeys (issue #13)

This section exists because the project's own premise is the thing most likely to be
wrong, and no amount of recall measurement tests it.

**Measured on the 5 labeled honeyslate commits** (`harness/accuracy.py`, current
main): selection picks a mean of **3.4 of 8 journeys**, so it avoids **57.5%** of
journey-runs. But the distribution is bimodal, not centred: **3 of 5 commits select
≤2 journeys** and **2 of 5 select ≥6** — one of them all 8, because a `Settings`
field change reaches everything. There is no "typical" saving to quote; a commit is
either narrow or nearly total.

**The null hypothesis:** at 8 journeys, `testgraph select` is close to
`codegraph_impact` plus a hand-written list, and 57.5% of a suite that takes minutes
is not worth a selector. If a full journey run is cheap, "run everything" is a
correct engineering answer and this repo is ceremony.

**What survives that hypothesis** — deliberately not the runtime saving:

1. **The target, not the time.** TDAD (arXiv:2603.17973v2) measured agents given TDD
   instructions *without* impact context regressing **worse than baseline** (9.94% vs
   6.08%). "Test your work" with no named target is actively harmful; the map supplies
   the target. That value does not scale with journey count — it is there at 8.
2. **The guards, not the list.** Most of this repo is now refusal machinery: the
   integrity spot-check that catches the corruption `codegraph sync` cannot repair,
   provenance that fails closed, the degrade-to-all-journeys paths, entry-drift
   against a live parse. Their value is preventing a *confident wrong answer*, which
   a "run everything" policy also prevents — but only if someone runs everything,
   which is exactly what does not happen under time pressure.
3. **The ranking.** When 8 of 8 are selected, `rank` and the `!` weak-edge flag still
   say which to check first and which not to trust. That is a different product from
   selection.

**Update — the second registry ran (#11) and the shape replicated.** signedintake:
mean 2.21 of 8 journeys, 72.3% of journey-runs avoided, **12 of 14 commits select <= 2
and 2 select all 8, with nothing in between**. Same bimodality, an independent
codebase, a different language, and better selectivity than honeyslate. So the shape
is how change lands in code, not a honeyslate quirk — and the "narrow or total"
pattern is what the guards and the ranking exist to serve.

**Update 2 — re-measured at 14 journeys (`harness/selectivity.py`, 2026-07-31).** The
numbers above are the retired 8-journey registry. At 14, per-commit indexes: mean
**2.64 of 14, 81.1% avoided**, 11 of 14 commits select <= 2, and **nothing selects all
14** — the ceiling is 11/14, with the histogram empty from 4 through 10. The gap in
the middle survived; "total" did not. Selectivity improved as the registry grew,
because the journeys added (a lifespan hook, a CLI script) sit off the shared-symbol
paths — which also means the effect should reverse for a registry grown *along* them.
The 20+ question below is still untested.

**What would falsify the whole thing:** if a second registry (#11) shows the same
bimodal shape at 20+ journeys — narrow commits stay narrow, shared-symbol commits
still collapse to everything — then the ceiling on selectivity is the codebase's
coupling, not the tool, and the honest move is to stop selling selection and ship the
map plus the guards. The results ledger (#10) is what would make that measurable
rather than arguable.

### Update 3 — the 20+ falsification ran, and it fired (2026-08-05)

The condition above was tested on two repos neither of which had a registry before, one
of them written by strangers. **It fired.** Selection rule and expected readings were
written to `~/.claude/plans/testgraph-20plus-journey-falsification.md` before the sweep,
because the prediction was already published and a hand-picked registry would prove
nothing.

**mealie** (`mealie-recipes/mealie`, an OUTSIDE repo — no authorship connection to
honeyslate or signedintake, which removes the same-author confound those two share).
23 drafted journeys, 40 commits, per-commit indexes:

| | value |
|---|---|
| mean selected | 1.15 / 23 (5.0%) |
| journey-runs avoided | 95.0% |
| histogram | **`{0: 38, 23: 2}`** |
| `recall_degraded` | 0 of 40 |

**There is no middle.** Not a gap with outliers on either side — literally two values,
nothing and everything. This is the "narrow or total" shape at 23 journeys, in code this
project has never seen, and it is the sharpest form of it measured so far.

**coriolis-local** (207 drafted journeys — full handler coverage, 60 commits):

| | all 60 commits | non-degraded (52) | **commits touching backend Python (25)** |
|---|---|---|---|
| mean selected | 46.88 / 207 | 22.25 / 207 | **95.00 / 207 (45.9%)** |
| journey-runs avoided | 77.4% | 89.3% | **54.1%** |
| ceiling | 207 / 207 | 189 / 207 (91%) | 207 / 207 |
| `<= 2` journeys | 36 of 60 | 36 of 52 | 5 of 25 |

Histogram over all 60: `{0:31, 1:5, 3:2, 12:4, 14:1, 25:3, 82:1, 183:1, 184:1, 185:1,
186:1, 189:1, 207:8}` — empty from 26 to 81 and from 83 to 182. The same bimodality,
with **11 of 25 backend-touching commits selecting >= 88% of the registry**.

**A methodology correction that applies to every number above this line.** The published
72.3% and 81.1% "journey-runs avoided" figures are means over *all* commits in the
window, including commits that touch no registered surface at all and therefore could
never select anything. On coriolis that difference is not cosmetic: 77.4% across all 60
commits versus **54.1%** across the 25 that touch backend Python. A commit that changes
only CI config is not evidence the selector is selective. The signedintake figures should
be read with the same discount until re-measured that way.

**A window-size correction to Update 2's reading.** At 30 commits coriolis looked like a
continuation of the trend — ceiling 82/207 (40%), nothing degraded. Extending the same
sweep to 60 found eight commits that degrade to all 207 and five more selecting 183–189.
The 30-commit result was a small-window artifact. Sweeps shorter than the repo's release
cadence do not see the shared-symbol commits, which is exactly the tail the question is
about.

**Coupling is not something a registry author can choose.** Before any sweep, the arm
construction measured coriolis's dependency structure: the shared core is **77 nodes**,
**174 of 207** handlers transitively reach **>= 90%** of it, and the floor across all 207
is **62 of 77**. There is no low-coupling 22-journey registry to build in that codebase —
the "isolated journeys" arm does not exist. That is the kill condition's mechanism stated
structurally rather than observed statistically.

**What survived, and it is the part TECHNICAL.md already argued for.** Of mealie's two
total-selection commits, one is genuine and canonical: a change to
`mealie/core/settings/settings.py` reaching all 23 journeys, the shared-config fan-out
this document has described since honeyslate. The other is a **single frontend locale
file** (`available-locales.ts`, one file, one seed) expanding to 8,721 impacted symbols
and all 23 journeys — and every one of them came back at **confidence 0.3 with
`verify_manually: true`**, the `HEURISTIC_CONFIDENCE` cap. The selector said "everything,
and trust none of it." That is B1's weak-edge flag doing exactly what it was built for,
on outside code, unprompted. By contrast coriolis's 82-journey selection from a two-file
CIDR/settings change came back at **confidence 0.9 across all 82, zero
`verify_manually`** — a genuine wide blast radius, correctly reported as one.

So: the selection claim does not survive at 20+ journeys, and the *ranking and
confidence* claim does. That is the split this document predicted under "What survives
that hypothesis" (points 2 and 3) and the evidence now points at it rather than at
selectivity.

Reproduce: `harness/couple.py` builds the arms, `harness/registries/*.json` are the
registries measured (all `approved: false` — machine-drafted measurement artifacts, never
product registries), and `harness/selectivity.py --bare ... --registry ...` runs the
sweep. `tests/test_couple.py` pins the one piece of new logic that could silently
invalidate the split: `footprint()` inverts `db.impacted_closure` exactly, checked
node-by-node against the real closure rather than by eye.

### Update 4 — trace-derived ground truth: the instrument works, the honeyslate run is blocked (2026-08-06)

Issue #12. Every recall number this project has published was scored against a
**static** oracle: five hand labels, or `harness/ast_oracle.py`, which is
independent of CodeGraph but still derived from source text. Both inherit static
analysis' blind spots — dynamic dispatch, `getattr`, decorator registries,
framework-driven entry. A trace has none of them, because it records what ran.

**The instrument** (`harness/tgtrace.py`, `harness/trace.py`,
`harness/ground_truth.py`) runs a target's own pytest suite under `sys.monitoring`
(PEP 669; `sys.setprofile` fallback below 3.12), recording every function entered
during each test *body* — not setup or teardown, because fixtures build the world
and everything they touch would land in every journey sharing a fixture. Tests map
to journeys through a hand-authored file, and the comparison is:

    traced_only = traced(J) − Dep(E)

where `Dep(E)` is `couple.footprint(entries)` — exactly the set of edits that would
cause testgraph to select J. A symbol in `traced_only` runs during J and cannot
select J. That is the silent-miss shape.

Three buckets, kept apart on purpose: `traced_only` (gated — a traversal gap),
`static_only` (reported, never gated — over-selection is the chosen direction, and
an uncovered branch produces these too, which is why this side cannot be a
precision score), and `unresolved` (traced symbols with no node at all — an
*indexing* gap, a different defect with a different fix).

**Demonstrated end to end on a controlled target.** `harness/fixtures/dyndemo` is
four modules where `routes.create` reaches `dyn.audit` only through
`getattr(mod, HOOK)`. Measured 2026-08-06:

```
1/1 journey(s) scored; 1 traced symbol(s) outside the static footprint
  J1  create a thing  ! SILENT-MISS SOURCE
      traced 5 symbol(s) -> 4 node(s); static footprint 7 (from 1 resolved entry symbol(s))
      traced_only 1   static_only 4   unresolved 0
        - audit (app/dyn.py)
```

The harness found the planted defect. That validates the instrument; it says
nothing about honeyslate.

**The honeyslate measurement did not run, and the reason is not a bug in this
code.** `backend/tests/conftest.py` declares `clean_db` as `autouse=True`, and it
opens `postgresql+psycopg://honeyslate@localhost:55433/honeyslate` before every
test. That port is **closed** on this machine (probed 2026-08-06), and Docker is
not installed under WSL, so `deploy/docker-compose.yml` cannot bring it up either.
`pytest --collect-only` finishes in 0.05s — collection imports fine — while a real
run blocks on the DB connect and was killed at 300s. The suite is not slow; it is
waiting for infrastructure that is not here.

This is the #8 decision arriving with its bill. testgraph deliberately does not own
an environment, so the trace harness inherits whatever the target needs to run, and
honeyslate needs a seeded Postgres. `harness/journey_tests_honeyslate.json` is
written and ready; the run needs a dev Postgres on :55433 and nothing else.

**The map is keyed on path suffixes, not on exact nodeids.** pytest nodeids are
relative to its *rootdir*, which for a target with no ini file falls back to the
invocation directory — so `--repo <r>/backend --tests tests` yields
`tests/test_auth.py::…` while `--repo <r> --tests backend/tests` yields
`backend/tests/test_auth.py::…` for the same test. Matching longest-suffix-first
makes the map independent of how the harness was invoked. Keying on exact
nodeids meant every test went unmapped under the other invocation form, every
journey reported `no_trace`, and the summary line read as a clean run.

**The map is coarse on purpose.** It labels at *file* level, never per test-nodeid:
a per-test label needs a reading of what each test asserts, which is the judgement
call #12 exists to replace, and a wrong per-test label is invisible in the result. A
file exercising several journeys is mapped to *all* of them, which inflates
`traced(J)` and therefore inflates `traced_only` — making the selector look worse.
Same argument as `ast_oracle.py`'s bare-name matching: an over-approximating oracle
makes the test harder to pass, never easier.

`tests/test_notifications.py` is left deliberately unmapped and is *reported* as
unmapped. A traced behaviour with no journey is a registry gap; quietly attaching
the digest mailer to J8 would hide it.

**Scope not delivered.** #12's full scope is traces *replacing* hand-labeled
journeys. This ships the measurement, not the replacement — and one hand-labeled
artifact survives by design, in its own visible file rather than as a heuristic
inside `ground_truth.py`.

## Known Limitations

- **Scope:** honeyslate is the only *approved* registry. `testgraph.propose`
  (issue #6) drafts one for any Python repo with decorator-style routes, but a
  draft is unreviewed by construction and says so on every run. Frontend files are
  *seeded* (issue #21), but no journey has a frontend entry point, so a frontend
  change is only visible where it reaches a backend entry.
- **The proposer reads Python decorators and Next.js conventions only.** A
  Django `urls.py`, a class-based view, Express/Fastify/tRPC, a pages-router
  `pages/api/*`, or any other framework yields no candidates. The run reports its
  blind spots, writes **nothing**, and exits 1 — an empty registry would be
  valid, approvable, and answer `NONE` for every change.
- **Precision on shared symbols:** a config/model edit fans out to most journeys
  by design (recall-first). Mean precision 0.68 on the labeled set; the low case
  is 0.38 (a `Settings` field change → all 8).
- **Registry completeness is manual:** a journey's entry set must include its
  wiring/lifecycle code, not just the leaf handler (the harness caught J8 missing
  `scheduler.start`). Missing entries cause silent under-selection.
- **Index integrity is the tool's soundness ceiling.** The guard mitigates but
  cannot fully verify a graph; a subtly wrong index yields wrong answers.
- **An index older than the change it is asked about is no longer trusted.**
  Seeds come from *line ranges*, so a changed file whose bytes differ from the
  indexed copy hands the diff's line numbers to whatever symbol used to occupy
  them — a neighbouring function, or nothing, and the answer stays confident
  while being narrower than the truth. `select` compares each changed file
  against `files.content_hash` and treats a mismatch as unmappable:
  `recall_degraded: true`, every journey listed, the file named. Detected by
  content hash, **not mtime** — `git checkout` rewrites mtimes without changing
  a byte, and `codegraph sync` leaves `indexed_at` alone when content is
  unchanged (it reports "Already up to date"), so an mtime rule fires on every
  branch switch and then never clears. The hash is what the indexer compares.
  The pre-push hook runs `codegraph sync` first so this stays the exceptional
  path rather than every push.
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
