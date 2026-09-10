"""Phase 4: run a pytest suite once and record which journeys its tests
ACTUALLY exercised — from the runtime trace, not from a static guess.

The ledger has had one writer since #10: a human (or /autorun) typing
`testgraph record --journey J3 --outcome fail`. That is the bottleneck the
ledger's emptiness has always come down to. This is the automatic writer:
`harness/plugin/tgtrace.py` already records every symbol that ran during each
test BODY, and the registry already says which symbols ARE each journey.
Intersect them and a plain, untagged pytest suite says which journeys it
covered, with no test author doing anything.

Attribution is against journey ENTRY symbols, not against a dependency
footprint — see `journey_entries` for the measurement that settled it. The
short version: a footprint answers "what could a change break", and using it
here made one CLI's tests appear to cover every journey in the repo.

`edge_provenance="trace"` on every row written here. Rows written the old way
carry no such field, and that asymmetry is deliberate: a human asserting "J3
failed" is a different kind of claim from this module INFERRING it from a
trace, and a later reader (`ledger.summarize`, a reconciler, issue #12's
Phase 5) must be able to tell them apart without guessing. See
`~/.claude/plans/testgraph-phase4-pytest-adapter.md`.

Deliberately NOT here, and each for a stated reason:

  - **Marker-based attribution** (`tg_J4` pytest markers, the Phase 3 tag
    convention). It needs a companion plugin to expose `item.own_markers`, and
    it produces nothing until every test is tagged. Trace attribution works on
    an untagged suite today — testgraph's own has zero `tg_*` markers.
  - **A generic runner driver** off Phase 3's `runners:` `select` template.
    There is no second adapter to generalize against yet, and a one-adapter
    abstraction is a guess rather than a design (verification-direction D3).
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import db as dbmod
from . import ledger
from . import registry as reg
from . import results as res

TRACE_PROVENANCE = "trace"

# Journey verdict precedence: one failing covering test breaks the journey,
# however many passed alongside it. `skip` only wins when nothing else ran.
_VERDICT_RANK = {"fail": 3, "pass": 2, "skip": 1}


def _join_key(nodeid):
    """Normalize a pytest nodeid (`tests/test_x.py::Cls::test_y`) to a dotted
    key (`tests.test_x.Cls.test_y`).

    This exists because the two halves of a run arrive keyed differently:
    tgtrace records nodeids, JUnit XML records `classname` + `name`, and
    nothing in the XML carries the nodeid back. Reconstructing a nodeid FROM
    the XML is ambiguous — `tests.test_x.Cls` gives no way to tell where the
    module path ends and the class begins — so both sides are flattened to
    the one shape that is unambiguous from either direction instead."""
    path, _, rest = nodeid.partition("::")
    if path.endswith(".py"):
        path = path[:-3]
    parts = [path.replace(os.sep, ".").replace("/", ".")]
    parts.extend(p for p in rest.split("::") if p)
    return ".".join(parts)


def _junit_key(classname, name):
    """The same dotted key, from a JUnit `<testcase>`'s two attributes."""
    return f"{classname}.{name}" if classname else name


def parse_junit(xml_source):
    """JUnit XML -> {join_key: run_result}. Accepts a path or raw XML text.

    Status comes from the child element pytest writes, and `error` is kept
    DISTINCT from `failure` here even though both collapse to a `fail` verdict
    downstream (`results.TO_LEDGER_VERDICT`) — a suite erroring in a fixture is
    a different diagnosis from one failing an assertion, and the raw record is
    the last place that distinction still exists."""
    if isinstance(xml_source, str) and xml_source.lstrip().startswith("<"):
        root = ET.fromstring(xml_source)
    else:
        root = ET.parse(xml_source).getroot()

    out = {}
    for case in root.iter("testcase"):
        if case.find("failure") is not None:
            status = "fail"
            node = case.find("failure")
        elif case.find("error") is not None:
            status = "error"
            node = case.find("error")
        elif case.find("skipped") is not None:
            status = "skip"
            node = case.find("skipped")
        else:
            status, node = "pass", None
        try:
            duration = float(case.get("time")) if case.get("time") else None
        except ValueError:
            duration = None
        key = _junit_key(case.get("classname", ""), case.get("name", ""))
        out[key] = res.run_result(
            "pytest",
            key,
            status,
            duration_s=duration,
            message=(node.get("message") if node is not None else None),
        )
    return out


def journey_entries(conn, registry):
    """{journey_id: set(entry_node_id)} — the symbols that ARE each journey.

    **Not** `harness/couple.py:footprint`, and the difference is the whole
    correctness of this module. A footprint is `Dep(E)`: everything a journey
    depends on, which is the right set for SELECTION ("what could a change
    break") and exactly the wrong one for ATTRIBUTION ("what did this test
    exercise"). Measured on testgraph itself: attributing by footprint made
    `tests/test_record.py` alone — 34 tests of one CLI — cover all six
    journeys, because every journey's footprint contains the shared core
    (`ledger.py`, `registry.py`), so any test touching `ledger.append`
    intersected all of them. Coverage that says "everything" says nothing.

    A test covers a journey when it actually RAN one of that journey's entry
    points. That is the same set `select` intersects its impacted closure
    against (`registry.resolve_entries`), so "journey J" means one thing in
    both directions rather than two."""
    per_journey = {}
    for node_id, jids in reg.resolve_entries(conn, registry).items():
        for jid in jids:
            per_journey.setdefault(jid, set()).add(node_id)
    return per_journey


def resolve_traced(conn, symbols):
    """[(relpath, qualname)] -> (node_ids, ambiguous).

    `ambiguous` lists the symbols that resolved to MORE than one node in their
    own file. They are still counted — dropping them would silently shrink
    coverage — but they are reported, because a bare-name match inside one file
    is the one place this mapping can be wrong, and a caller deciding whether
    to trust a thin coverage margin needs to know it happened.

    The last dotted segment of a qualname is what codegraph stores as `name`
    (`Cls.method` is indexed as `method`), and the relative path scopes it, so
    same-named functions in different files never collide here."""
    node_ids, ambiguous = set(), []
    for relpath, qualname in symbols:
        bare = qualname.rsplit(".", 1)[-1]
        ids = dbmod.resolve_symbol(conn, bare, relpath)
        if not ids:
            continue
        if len(ids) > 1:
            ambiguous.append((relpath, qualname))
        node_ids.update(ids)
    return node_ids, ambiguous


def attribute(conn, trace_tests, entries, verdicts):
    """Join a run's two halves into per-journey coverage.

    `trace_tests` is tgtrace's `{"nodeid": [[relpath, qualname], ...]}`;
    `verdicts` is `parse_junit`'s output. Returns a dict carrying the
    per-journey verdict plus the three ways this join can be incomplete —
    each reported rather than swallowed, because every one of them makes
    coverage look SMALLER than it was, and a silently small answer from a
    recall-first tool is the failure mode this project exists to avoid."""
    covered = {}                 # jid -> {verdict: count}
    ambiguous = []
    traced_without_verdict = []
    tests_covering_nothing = 0

    for nodeid, symbols in trace_tests.items():
        key = _join_key(nodeid)
        result = verdicts.get(key)
        if result is None:
            traced_without_verdict.append(nodeid)
            continue
        verdict = res.TO_LEDGER_VERDICT[result["status"]]
        node_ids, amb = resolve_traced(conn, [tuple(s) for s in symbols])
        ambiguous.extend(amb)

        hit_any = False
        for jid, entry_ids in entries.items():
            if node_ids & entry_ids:
                hit_any = True
                covered.setdefault(jid, {}).setdefault(verdict, 0)
                covered[jid][verdict] += 1
        if not hit_any:
            tests_covering_nothing += 1

    journeys = {
        jid: max(counts, key=lambda v: _VERDICT_RANK.get(v, 0))
        for jid, counts in covered.items()
    }
    return {
        "journeys": journeys,
        "covering_tests": {jid: sum(c.values()) for jid, c in covered.items()},
        "ambiguous_symbols": ambiguous,
        "traced_without_verdict": traced_without_verdict,
        "tests_covering_no_journey": tests_covering_nothing,
        "verdicts_without_trace": sorted(
            set(verdicts) - {_join_key(n) for n in trace_tests}
        ),
    }


def write_outcomes(repo_name, commit, attribution, append=None):
    """One ledger row per covered journey, each tagged `edge_provenance`.

    `append` is injectable so tests never touch the real ledger — the same
    reason `record.add_outcome` is the only other writer and is equally
    narrow."""
    append = append or ledger.append
    written = []
    for jid, verdict in sorted(attribution["journeys"].items(), key=lambda kv: reg.journey_sort_key(kv[0])):
        row = ledger.outcome_row(
            repo_name,
            commit,
            jid,
            verdict,
            note=f"{attribution['covering_tests'][jid]} covering test(s), trace-derived",
            edge_provenance=TRACE_PROVENANCE,
        )
        if append(row):
            written.append(row)
    return written


def plugin_dir(start=None):
    """Where `tgtrace.py` lives, or None.

    The installed wheel ships `testgraph/` ONLY — `harness/` is a repo
    artifact with no import contract (the packaging decision recorded in
    `testgraph-public-release.md`). So this resolves relative to the source
    checkout and returns None rather than guessing when it is not there; the
    caller turns that into one clear error instead of an opaque pytest
    `-p tgtrace` import failure twenty lines into a subprocess log."""
    root = start or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, "harness", "plugin")
    return candidate if os.path.isfile(os.path.join(candidate, "tgtrace.py")) else None


def run(repo, registry_path=None, db_path=None, pytest_args=(), commit=None,
        append=None, _runner=None):
    """Run the suite, attribute journeys from the trace, write the rows.

    Returns (summary, error). `error` is a string on any refusal; a suite whose
    tests FAIL is not an error — a red suite is exactly the signal this exists
    to record."""
    registry_path = registry_path or reg.resolve_for_repo(repo)
    if registry_path is None:
        return None, (
            f"no journey registry for {reg.repo_name(repo)} — looked in "
            f"{reg.where_it_looked(repo)}"
        )
    try:
        registry = reg.load(registry_path)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {registry_path}: {exc}"

    plugins = plugin_dir()
    if plugins is None:
        return None, (
            "cannot find harness/plugin/tgtrace.py — it ships with the source "
            "checkout, not the installed wheel, so this adapter needs to run "
            "from a checkout"
        )

    db_path = db_path or os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.isfile(db_path):
        return None, f"no codegraph index at {db_path} — run `codegraph init` in {repo}"

    sha = ledger.resolve_commit(repo, commit or "HEAD")
    if sha is None:
        return None, (
            f"cannot resolve {commit or 'HEAD'!r} in {repo} — an unjoinable key "
            f"would sit in `unasked` forever"
        )

    with tempfile.TemporaryDirectory() as tmp:
        trace_path = os.path.join(tmp, "trace.json")
        junit_path = os.path.join(tmp, "junit.xml")
        env = dict(os.environ)
        env["TGTRACE_OUT"] = trace_path
        env["TGTRACE_ROOT"] = os.path.abspath(repo)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (plugins, env.get("PYTHONPATH")) if p
        )
        cmd = [
            sys.executable, "-m", "pytest", "-p", "tgtrace",
            f"--junitxml={junit_path}", *pytest_args,
        ]
        runner = _runner or (
            lambda: subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True)
        )
        proc = runner()

        if not os.path.isfile(junit_path):
            tail = (getattr(proc, "stderr", "") or "")[-400:]
            return None, f"pytest wrote no JUnit XML (exit {getattr(proc, 'returncode', '?')}): {tail}"
        if not os.path.isfile(trace_path):
            return None, (
                "pytest ran but tgtrace wrote nothing — the plugin was not "
                "loaded (check that -p tgtrace resolved on PYTHONPATH)"
            )

        import json
        with open(trace_path) as f:
            trace = json.load(f)
        verdicts = parse_junit(junit_path)

    conn = dbmod.connect(db_path)
    entries = journey_entries(conn, registry)
    attribution = attribute(conn, trace.get("tests", {}), entries, verdicts)
    written = write_outcomes(reg.repo_name(repo), sha, attribution, append=append)

    summary = dict(attribution)
    summary.update({
        "repo": reg.repo_name(repo),
        "commit": sha,
        "rows_written": len(written),
        "pytest_exit": getattr(proc, "returncode", None),
        "backend": trace.get("backend"),
        "journeys_registered": len(entries),
    })
    return summary, None


def render(summary, registry=None):
    """Human-readable run report.

    Names the journeys with NO covering test as loudly as the covered ones. A
    coverage report that lists only what it found reads as completeness; the
    gap is the actionable half — measured on testgraph itself, J6 (the harness
    CLIs) has zero covering tests in a 359-test suite, and nothing but this
    line would say so."""
    lines = [
        f"testgraph pytest-adapter[{summary['repo']}] @ {summary['commit'][:9]}  "
        f"(pytest exit {summary['pytest_exit']}, {summary['backend']})"
    ]
    covered = summary["journeys"]
    names = {}
    if registry:
        names = {j: (v or {}).get("name", "") for j, v in registry.get("journeys", {}).items()}

    for jid in sorted(covered, key=reg.journey_sort_key):
        lines.append(
            f"  {jid}  {names.get(jid, ''):<40.40} {covered[jid]:<5} "
            f"{summary['covering_tests'][jid]} covering test(s)"
        )

    uncovered = sorted(
        set(names) - set(covered) if names else (), key=reg.journey_sort_key
    )
    if uncovered:
        lines.append(
            f"  NO COVERING TEST: {', '.join(uncovered)} — this suite never ran "
            f"their entry points, so this run says nothing about them"
        )

    if summary.get("dry_run"):
        lines.append(
            f"  would write {len(covered)} ledger row(s) tagged "
            f"edge_provenance={TRACE_PROVENANCE} — DRY RUN, nothing written"
        )
    else:
        lines.append(
            f"  {summary['rows_written']} ledger row(s) written, tagged "
            f"edge_provenance={TRACE_PROVENANCE}"
        )
    lines.append(
        f"  {summary['tests_covering_no_journey']} test(s) covered no journey "
        f"(they exercise internals, not entry points)"
    )
    if summary["ambiguous_symbols"]:
        lines.append(
            f"  ! {len(summary['ambiguous_symbols'])} symbol(s) resolved to more "
            f"than one node in their own file — coverage may be overstated"
        )
    if summary["traced_without_verdict"]:
        lines.append(
            f"  ! {len(summary['traced_without_verdict'])} traced test(s) had no "
            f"JUnit verdict — coverage understated"
        )
    if summary["verdicts_without_trace"]:
        lines.append(
            f"  ! {len(summary['verdicts_without_trace'])} test(s) had a verdict "
            f"but no trace — coverage understated"
        )
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(
        prog="testgraph.pytest_adapter",
        description="run a pytest suite and record which journeys it exercised",
        epilog=(
            "Put pytest's own flags after `--`, or argparse claims them:\n"
            "  python3 -m testgraph.pytest_adapter --repo . --dry-run -- tests/ -q"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="path to the target repo")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--db", default=None, help="codegraph db (default: <repo>/.codegraph/codegraph.db)")
    ap.add_argument("--commit", default=None, help="commit the run exercised (default: HEAD)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="attribute and report, but write NO ledger rows",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "pytest_args",
        nargs="*",
        help="passed through to pytest (e.g. tests/ -q -k foo)",
    )
    args = ap.parse_args(argv)

    summary, err = run(
        args.repo,
        registry_path=args.registry,
        db_path=args.db,
        commit=args.commit,
        pytest_args=tuple(args.pytest_args),
        # Refusing the write keeps `rows_written` truthful (0) rather than
        # counting rows that never reached the file — a dry run must not
        # report the same number a real one would.
        append=(lambda row: False) if args.dry_run else None,
    )
    if err:
        print(f"testgraph pytest-adapter: {err}", file=sys.stderr)
        return 2
    summary["dry_run"] = bool(args.dry_run)

    if args.json:
        print(_json.dumps(summary, indent=2, sort_keys=True))
        return 0

    registry = None
    path = args.registry or reg.resolve_for_repo(args.repo)
    if path:
        try:
            registry = reg.load(path)
        except (OSError, ValueError):
            registry = None
    print(render(summary, registry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
