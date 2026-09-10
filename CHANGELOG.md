# Changelog

Notable changes to testgraph. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions come from the git tag (hatch-vcs), so a heading here is a tag that
exists. GitHub Releases are auto-generated from commits; this file is the
curated record of what actually changed for a user of the tool.

## [Unreleased]

Entries land here as they merge to `main`, not when a PR opens — everything
below is on `main` or in the branch that introduced this file.

### Added

- **`testgraph.reconcile`** — two-sided reconciliation of the static graph
  against a real test run, classifying every disagreement as a **STATIC MISS**
  (a symbol the journey's tests executed, outside its static footprint) or a
  **PHANTOM EDGE** (a `calls` edge no syntax in the caller can denote, which
  the run never exercised). Each defect carries its own burden of proof: a
  trace proves presence and never absence, a source oracle proves absence of
  support and never presence, so a phantom needs both witnesses and a static
  miss needs only the run. Every phantom reports which journeys depend on the
  callee *only* through it.

  Run against testgraph itself, this measures the two limitations recorded at
  0.1.0 rather than asserting them — and the answer differs by index, which is
  the finding. On a **freshly built** index: 91 static misses across 7
  journeys and **zero** phantom edges. The alias blind spot is still real
  (`db.resolve_symbol` and `db.connect` have no inbound caller at all, so
  `hook.py`'s `reg.repo_name(repo)` reaches no journey), but `ledger.append`'s
  inbound edges are now all genuine — the extractor fix landed.

  On the same commit's **incrementally synced** index, the same run reported 6
  confirmed phantom `ledger.append` edges, every one hand-verified as a real
  fabrication. `codegraph sync` re-extracts changed files only, so edges
  produced by a superseded extractor survive a version upgrade indefinitely.
  The integrity guard catches a *stale file*; it does not catch a stale
  *edge*. A phantom count that drops to zero after a full `codegraph index`
  is that defect showing itself.

- **pytest adapter** (`python3 -m testgraph.pytest_adapter`) — runs a suite
  once and records which journeys it exercised, from the runtime trace rather
  than from a static guess or a test tag. Rows are tagged
  `edge_provenance: "trace"`; rows a human wrote carry no such field. Journeys
  with no covering test are named as loudly as covered ones.
  `--trace-out` keeps the raw trace for `reconcile`.

- **Normalized result record** (`testgraph.results`) — the per-test-run shape
  every runner adapter produces, the `tg_{journey}` tag convention, and a
  `runners:` spec, kept separate from any one adapter's mechanics.

- **MCP stdio server** (`python3 -m testgraph.mcp`), exposing `testgraph_impact`
  and `testgraph_journeys` to a coding agent. Hand-rolled rather than built on
  the `mcp` SDK: MCP servers are spawned once per agent session, so idle size is
  multiplied by every open editor window. Measured 15.2 MB RSS after a full
  handshake, against 62–69 MB for a typical SDK-based Python server. Register it
  per repo, not globally.

- **Stale-edge integrity check** (#85). `codegraph sync` re-extracts changed
  files only, so when the extractor itself changes, edges it produced for
  *unchanged* files are never re-derived — an index can be newer than a fix
  and contain none of its output, indefinitely. The existing freshness check
  cannot see this and never could: it asks whether a **file** changed, and
  every file whose edges are wrong is by construction unchanged.

  Registries now pin `codegraph_extraction_version` alongside
  `codegraph_schema_version`, and `integrity.check` warns when the index was
  built by an older extractor, naming `codegraph index` (**not** `sync`, which
  is what caused it). A warning rather than a block — stale edges cost recall,
  not trust in the schema — but `select.none_is_unknown` takes warnings
  wholesale, so an empty journey list off a stale-edge index now reads UNKNOWN
  instead of as a clean bill.

  It found two on its first run: honeyslate and signedintake were both at
  extraction version 25 against a current 26.

- **`testgraph.verify`** — a diff in, the tests for the journeys it endangers
  run, a verdict out.

  ```bash
  python3 -m testgraph.verify --repo . --base origin/main -- tests/ -q
  ```

  Attribution is **declared**, not inferred. `testgraph.results.covers("J2")`
  marks a test as exercising a journey end to end; the tgtrace plugin turns
  that into a real `tg_J2` pytest marker at collection time, so
  `pytest -m 'tg_J1 or tg_J2'` does the selection and no attribution map is
  stored (a map that is not stored cannot go stale).

  Inferred attribution was measured and rejected for this job.
  `harness/unit_vs_journey.py` executes one journey both ways and compares:
  for J1, the 20 tests Phase 4 credits reach 14 symbols where a real invocation
  reaches 40, sharing 6, because `tests/test_hook.py` stubs out `sel.select`
  and `reg.resolve_for_repo` on every real path. Selecting tests from that
  would take a diff touching `db.py:impacted_closure`, select J1, run 20 tests
  that never execute it, and print PASS.

  A declaration is an assertion, so it is checked twice: every run, a marked
  test's trace must reach the journey's entry symbols (unsupported claims are
  reported, not credited); and periodically, `unit_vs_journey.py` checks the
  deeper claim that a test entering the journey is not mocking everything
  beneath it.

  **A selected journey with no declared test is the headline and has its own
  exit code** — 0 every selected journey ran and passed, 1 a journey failed,
  2 refused (untrustworthy index), 3 incomplete. Measured on this repo, a
  full-registry selection reports `NO JOURNEY-LEVEL TEST: J1, J5, J6, J7` and
  exits 3.

- **`covers()` cannot be `@pytest.mark`** — CI is `python3 -m unittest discover`
  on a stdlib-only checkout with no pytest, so importing pytest in a test module
  would break the runner the suite actually uses. It sets a plain attribute; the
  plugin reads it by name and never imports this package, which is what lets
  tgtrace keep running against repos that have never heard of testgraph.

### Changed

- **The registry is looked for in the repo it describes.** Search order is
  `$TESTGRAPH_JOURNEYS_DIR`, then `<repo>/.testgraph/journeys`, then the
  package-relative `journeys/`. `propose` drafts into the repo for the same
  reason, and every not-found message now names the directories searched.
- The registry `target` check applies in all three locations. A registry copied
  from another project and left unedited is refused, not used — it is the case
  that used to report its own mismatch as a stale index.

### Fixed

- **A `pip install` could never find a registry.** `resolve_for_repo` searched
  only a package-relative `journeys/`, which resolves to `site-packages/journeys`
  in a wheel and does not exist — so every repo answered "no journey registry
  found" forever, including through the MCP server, which would have returned
  that to every agent that ever called it. Found by installing the wheel and
  running it, not by reading.

## [0.1.0] — 2026-09-03

First public release. The repo went public the same day.

### Added

- `testgraph.select` — a git diff in, the ranked user journeys it could have
  broken out. Confidence propagates as `max over paths of (min over edges)`.
- `testgraph.propose` — drafts a registry for a new repo from Python route
  decorators and Next.js conventions, marked `approved: false` until a human
  reads it. An unapproved registry runs loudly, never silently.
- `testgraph.export` — the static journey map the pre-commit skill reads.
- `testgraph.hook` + `hooks/install.sh` — a `pre-push` consumer that never fails
  a push. A selector nobody calls is a selector nobody can learn from.
- `testgraph.record` + `testgraph.ledger` — selections and journey outcomes in
  one store, because the number worth having ("a journey failed and the
  selection did not name it") is a join across the two.
- `testgraph.integrity` — blocks on an index it cannot trust rather than
  answering from one. Degrades toward *test more*, never toward silence.
- Measurement harness: accuracy against hand-labeled commits, seeded mutation
  recall, selectivity, and trace-derived ground truth.

### Known limitations at 0.1.0

Both recorded in `journeys/testgraph.json` with the measurements behind them:

- CodeGraph does not resolve calls through a module bound under an **alias**, so
  `db.py` and `registry.py` reach no journey and a change to either can answer
  `NONE` with `status: OK`.
- `ledger.append`'s inbound edges are **fabricated** by the extractor dropping
  non-identifier receivers — cross-language, reproduced in Python, JS and Go.
  Its spot-check is suspended rather than deleted, so the reason survives.

[Unreleased]: https://github.com/inth3shadows/testgraph/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/inth3shadows/testgraph/releases/tag/v0.1.0
