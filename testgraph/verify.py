"""Phase 6: a diff in, the tests for the journeys it endangers run, a verdict out.

    python3 -m testgraph.verify --repo . --base origin/main

The whole design turns on one measured fact. Phase 4 attributes a test to a
journey by INFERENCE — the test's runtime trace touched one of the journey's
entry symbols. `harness/unit_vs_journey.py` measured what that inference is
worth, and for J1 (the pre-push hook) it is worth nothing: its 20 credited tests
reach 14 symbols where a real invocation reaches 40, sharing only 6, because
`tests/test_hook.py` carries 15 `mock.patch` calls and stubs out `sel.select`
and `reg.resolve_for_repo` on every path that would run real code.

So selecting tests from inferred attribution would take a diff touching
`db.py:impacted_closure`, select J1, run those 20 tests — not one of which
executes `impacted_closure` — and print PASS. A confident green over an
untested change is the failure this project defines itself against, so this
module does not do that.

Attribution here is DECLARED (`testgraph.results.covers`), and a declaration is
an assertion, so it is checked two ways:

  1. **Here, every run, free.** A marked test's own trace must reach the claimed
     journey's ENTRY symbols. A test asserting `tg_J1` that never enters
     `hook.main/run/render` is reported `unvalidated` and its journey is not
     credited.
  2. **`harness/unit_vs_journey.py`, periodically.** The deeper claim: that a
     test which DOES enter the journey is not then mocking everything beneath
     it. Level 1 cannot catch that — J1's tests do enter `hook.run` — which is
     precisely why both exist.

A journey selected with no marked test is the headline, not a footnote, and it
gets its own exit code. Measured on this repo: J1 has 20 credited tests and
zero journey-level ones, so `verify` on a diff touching the hook reports exactly
that rather than a green.
"""
import os
import subprocess
import sys
import tempfile

from . import db as dbmod
from . import ledger
from . import pytest_adapter as pa
from . import registry as reg
from . import results as res
from . import select as sel

#: Rows written here are DECLARED and validated, not inferred. Phase 4's bare
#: `"trace"` stays valid and means the older unqualified inference (D2
#: mitigation 1: `static | trace-unit | trace-journey`).
JOURNEY_PROVENANCE = "trace-journey"

EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_REFUSED = 2
#: At least one SELECTED journey had no declared test. Distinct from OK on
#: purpose: "every selected journey passed" and "some selected journey had
#: nothing to run" are different answers, and a shared exit code lets a CI job
#: read the second as the first — this module's own failure mode, relocated
#: into an integer.
EXIT_INCOMPLETE = 3


def marker_expression(journeys, template=None):
    """`tg_J1 or tg_J2` — a pytest `-m` expression for these journeys.

    Uses Phase 3's tag convention rather than a second spelling of it, so the
    marker a test author writes and the marker this selects on cannot drift.
    `or` because a journey set is a union: any test covering ANY selected
    journey is in scope."""
    template = template or res.DEFAULT_TAG_CONVENTIONS["pytest"]
    return " or ".join(res.format_tag(template, j) for j in journeys)


def validate(conn, registry, trace, verdicts):
    """Check every declared claim against what the test actually ran.

    Returns (credited, unvalidated, per_journey_tests) where `credited` maps a
    journey to its verdict and `unvalidated` lists (nodeid, journey) claims the
    trace does not support.

    A claim is supported when the test executed one of the journey's ENTRY
    symbols — the same rule `select` resolves entries with, so "journey J"
    means one thing in both directions. Note what this deliberately does NOT
    check: how much of the journey ran after entry. That is
    `harness/unit_vs_journey.py`'s job, and conflating the two would let this
    module claim a guarantee it cannot make."""
    entries = pa.journey_entries(conn, registry)
    credited, unvalidated, per_journey = {}, [], {}

    for nodeid, declared in (trace.get("declared") or {}).items():
        if not declared:
            continue
        symbols = trace.get("tests", {}).get(nodeid)
        if symbols is None:
            # Collected and declared, but never ran a body — deselected by a
            # `-k`, skipped, or errored in setup. Not a false claim; no evidence
            # either way, so it credits nothing and accuses nothing.
            continue
        node_ids, _amb = pa.resolve_traced(conn, [tuple(s) for s in symbols])
        result = verdicts.get(pa._join_key(nodeid))
        for jid in declared:
            if not (node_ids & entries.get(jid, set())):
                unvalidated.append((nodeid, jid))
                continue
            per_journey.setdefault(jid, []).append(nodeid)
            if result is None:
                continue
            verdict = res.TO_LEDGER_VERDICT[result["status"]]
            prior = credited.get(jid)
            if prior is None or pa._VERDICT_RANK[verdict] > pa._VERDICT_RANK[prior]:
                credited[jid] = verdict
    return credited, unvalidated, per_journey


def _summary(repo, head, selection, **extra):
    """Every exit from `run` goes through here.

    The three returns — refused, nothing-selected, and the full run — used to
    build their dicts independently, and the short two omitted `repo`, so the
    report rendered `testgraph verify[?]` for a NONE answer. Cosmetic in the
    text; not cosmetic in `--json`, where a consumer reading `summary["repo"]`
    gets a KeyError on exactly the quiet runs it is most likely to be
    aggregating. A shared shape is the fix, not a fourth copy of the key."""
    base = {
        "repo": reg.repo_name(repo),
        "commit": ledger.resolve_commit(repo, head),
        "selection": selection,
        "journeys": [],
        "marker_expression": None,
        "collected": 0,
        "credited": {},
        "unvalidated": [],
        "per_journey": {},
        "uncovered": [],
        "rows_written": 0,
        "pytest_exit": None,
    }
    base.update(extra)
    return base


def run(repo, base, head="HEAD", registry_path=None, db_path=None,
        pytest_args=(), append=None, _runner=None):
    """Select, run the declared tests, validate the declarations, report.

    Returns (summary, error). A red suite is not an error — it is the signal."""
    registry_path = registry_path or reg.resolve_for_repo(repo)
    if registry_path is None:
        return None, (f"no journey registry for {reg.repo_name(repo)} — looked "
                      f"in {reg.where_it_looked(repo)}")
    db_path = db_path or os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.isfile(db_path):
        return None, f"no codegraph index at {db_path} — run `codegraph init` in {repo}"
    plugins = pa.plugin_dir()
    if plugins is None:
        return None, ("cannot find harness/plugin/tgtrace.py — it ships with "
                      "the source checkout, not the installed wheel")

    selection = sel.select(repo, base, head, db_path, registry_path,
                           strict_registry=False)
    # `status` is the documented contract; `blocking` is only present when it
    # fires, so reading the list alone would depend on a key that may be absent.
    if selection.get("status") == "BLOCKED" or selection.get("blocking"):
        # The index is not trustworthy. Running tests off it would produce a
        # verdict about a journey set chosen from a graph the guard refused.
        return _summary(repo, head, selection, refused=True), None

    # `select` returns journey dicts keyed `id` (with name/rank/confidence);
    # the plain-string form is what a caller passing a hand-built selection uses.
    journeys = [j["id"] if isinstance(j, dict) else j
                for j in selection.get("journeys", [])]
    registry = reg.load(registry_path)
    conn = dbmod.connect(db_path)

    if not journeys:
        return _summary(repo, head, selection), None

    expr = marker_expression(journeys)
    with tempfile.TemporaryDirectory() as tmp:
        trace_path = os.path.join(tmp, "trace.json")
        junit_path = os.path.join(tmp, "junit.xml")
        env = dict(os.environ)
        env["TGTRACE_OUT"] = trace_path
        env["TGTRACE_ROOT"] = os.path.abspath(repo)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (plugins, env.get("PYTHONPATH")) if p
        )
        cmd = [sys.executable, "-m", "pytest", "-p", "tgtrace",
               "-m", expr, f"--junitxml={junit_path}", *pytest_args]
        runner = _runner or (lambda: subprocess.run(
            cmd, cwd=repo, env=env, capture_output=True, text=True))
        proc = runner()

        import json
        trace = {}
        if os.path.isfile(trace_path):
            with open(trace_path) as fh:
                trace = json.load(fh)
        verdicts = {}
        if os.path.isfile(junit_path):
            verdicts = pa.parse_junit(junit_path)

    credited, unvalidated, per_journey = validate(conn, registry, trace, verdicts)
    uncovered = [j for j in journeys if j not in per_journey]

    written = []
    append = append or ledger.append
    sha = ledger.resolve_commit(repo, head)
    for jid, verdict in sorted(credited.items(), key=lambda kv: reg.journey_sort_key(kv[0])):
        row = ledger.outcome_row(
            reg.repo_name(repo), sha, jid, verdict,
            note=f"{len(per_journey[jid])} declared test(s), validated against entries",
            edge_provenance=JOURNEY_PROVENANCE,
        )
        if append(row):
            written.append(row)

    return _summary(
        repo, head, selection,
        journeys=journeys,
        marker_expression=expr,
        collected=len(verdicts),
        credited=credited,
        unvalidated=unvalidated,
        per_journey={k: sorted(v) for k, v in per_journey.items()},
        uncovered=uncovered,
        rows_written=len(written),
        pytest_exit=getattr(proc, "returncode", None),
        commit=sha,
    ), None


def exit_code(summary):
    """OK / tests-failed / nothing-to-run, as three distinct answers.

    `EXIT_NOTHING_TO_RUN` exists because "every selected journey passed" and
    "no selected journey had a test" are both quiet, and a shared code would
    let a CI job read the second as the first — which is the whole failure this
    module is built against, relocated into an integer."""
    if summary.get("refused"):
        return EXIT_REFUSED
    if any(v == "fail" for v in summary.get("credited", {}).values()):
        return EXIT_TESTS_FAILED
    # ANY uncovered journey, not merely all of them. The first version of this
    # returned OK whenever something was credited, and on its first real run it
    # printed "NO JOURNEY-LEVEL TEST: J1, J5, J6, J7" and exited 0 — four
    # journeys unverified behind a green from the other four. A partial answer
    # is not a pass, and the exit code is what a CI job actually reads.
    if summary.get("uncovered"):
        return EXIT_INCOMPLETE
    if summary.get("journeys") and not summary.get("credited"):
        return EXIT_INCOMPLETE
    return EXIT_OK


def render(summary, registry=None):
    if summary.get("refused"):
        lines = [f"testgraph verify[{summary.get('repo', '?')}]: REFUSED — "
                 f"the index is not trustworthy"]
        lines += [f"  BLOCKED: {b}"
                  for b in summary["selection"].get("blocking", [])]
        lines.append("  No tests were run. A verdict off a graph the guard "
                     "refused would describe a journey set chosen from it.")
        return "\n".join(lines)

    names = {}
    if registry:
        names = {j: (v or {}).get("name", "")
                 for j, v in registry.get("journeys", {}).items()}
    sel_ = summary["selection"]
    lines = [f"testgraph verify[{summary.get('repo', '?')}] "
             f"{sel_['base']}..{sel_['head']}"]
    for w in sel_.get("warnings", []):
        lines.append(f"  WARN: {w}")

    if not summary["journeys"]:
        lines.append("  journeys selected: NONE — nothing to verify")
        for r in sel.none_is_unknown(sel_):
            lines.append(f"    UNKNOWN, not verified-safe: {r}")
        return "\n".join(lines)

    lines.append(f"  journeys selected: {', '.join(summary['journeys'])}")
    lines.append(f"  pytest -m '{summary['marker_expression']}'  "
                 f"-> {summary['collected']} test(s), exit {summary['pytest_exit']}")
    for jid in sorted(summary["credited"], key=reg.journey_sort_key):
        lines.append(f"    {jid}  {names.get(jid, ''):<38.38} "
                     f"{summary['credited'][jid]:<5} "
                     f"{len(summary['per_journey'][jid])} declared test(s)")
    if summary["uncovered"]:
        lines.append(
            f"  ! NO JOURNEY-LEVEL TEST: {', '.join(summary['uncovered'])} — "
            f"selected, and nothing declared `covers(...)` for them. This run "
            f"says NOTHING about those journeys; it is not a pass."
        )
    if summary["unvalidated"]:
        lines.append(
            f"  ! {len(summary['unvalidated'])} declaration(s) the trace does "
            f"not support — the test ran but never entered the journey it "
            f"claims:"
        )
        for nodeid, jid in summary["unvalidated"][:8]:
            lines.append(f"      {jid}  {nodeid}")
    lines.append(f"  {summary['rows_written']} ledger row(s) written, tagged "
                 f"edge_provenance={JOURNEY_PROVENANCE}")
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(
        prog="testgraph.verify",
        description="run the declared tests for the journeys a diff endangers",
        epilog="Exit: 0 every selected journey ran and passed, 1 a journey "
               "failed, 2 refused (untrustworthy index), 3 incomplete — at "
               "least one selected journey had no declared test.\n"
               "Put pytest's own flags after `--`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="run and report, but write NO ledger rows")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("pytest_args", nargs="*")
    args = ap.parse_args(argv)

    summary, err = run(
        args.repo, args.base, args.head,
        registry_path=args.registry, db_path=args.db,
        pytest_args=tuple(args.pytest_args),
        append=(lambda row: False) if args.dry_run else None,
    )
    if err:
        print(err)
        return EXIT_REFUSED
    if args.json:
        print(_json.dumps(summary, indent=1, sort_keys=True, default=str))
    else:
        registry_path = args.registry or reg.resolve_for_repo(args.repo)
        registry = reg.load(registry_path) if registry_path else None
        print(render(summary, registry))
    return exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
