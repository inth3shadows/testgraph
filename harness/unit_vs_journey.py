"""The Phase 6 gate: execute ONE journey both ways and compare the symbol sets.

D2 mitigation 4 of `~/.claude/plans/testgraph-verification-direction.md` makes
this a precondition, not a nicety: *at least one journey must be executed both
ways (unit and real) and the edge sets compared, before pytest traces are
trusted as ground truth.*

The failure it guards against is documented in this repo, in writing, about this
repo. `harness/journey_tests_testgraph.json` records that `tests/test_hook.py`
`mock.patch.object`s both `hook.sel.select` and `hook.reg.resolve_for_repo` on
every path that would otherwise run real code, so its trace never leaves
`hook.py` + ledger. A trace like that describes a TEST-shaped execution
topology. Attributing tests to journeys from traces like that, then selecting
tests to run from that attribution, would run the wrong tests and print a
confident green — the exact failure this project exists to prevent.

Method
------
UNIT  the tests Phase 4's attribution credits to the journey (they executed one
      of its ENTRY symbols), traced, symbols unioned.
REAL  the journey's entry function invoked in-process against a real repo, a
      real codegraph index and a real diff — no mocks, no fixtures — traced
      through the same collector.

In-process rather than as a subprocess deliberately: a process boundary adds
nothing the trace can see and costs the ability to trace at all. For a CLI tool,
calling the entry with real arguments against real inputs IS the journey.

Reading the result
------------------
`unit - real` being large is EXPECTED and harmless. Unit tests exercise error
paths, guards and helpers that one real invocation does not reach; that is what
a test suite is for.

**`real - unit` is the number that decides the phase.** It is product code the
journey genuinely runs that no test credited to that journey ever touches. If it
is large and central, pytest unit traces are not ground truth for attribution,
and `testgraph verify` must attribute from declared markers instead.

A measurement, not a test — it needs a real index and CI has no codegraph, the
same reason `accuracy.py` and `seed_regressions.py` live here rather than in
`tests/`.

Usage:
    python3 harness/unit_vs_journey.py --repo <path> --trace <tgtrace.json> \
        [--journey J2] [--db PATH] [--registry PATH] [--base HEAD~1] [--json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from testgraph import db as dbmod  # noqa: E402
from testgraph import hook  # noqa: E402
from testgraph import reconcile as rec  # noqa: E402
from testgraph import registry as reg  # noqa: E402
from testgraph import select as sel  # noqa: E402

# The journey entry each id is executed through for the REAL half. Only the
# entries whose "real" invocation needs nothing but a repo and an index are
# listed; a journey that needs an environment belongs to a later phase, and
# guessing an invocation would measure the guess.
REAL_INVOCATIONS = {
    "J2": "select.select(repo, base, head, db, registry) — the whole selector",
    # The adversarial case, and the reason this gate exists. J1's tests
    # `mock.patch.object` both `hook.sel.select` and `hook.reg.resolve_for_repo`
    # on every path that would run real code, so their trace never leaves
    # `hook.py` + ledger. If unit traces are test-shaped anywhere in this repo,
    # it is here.
    #
    # `hook.run` rather than `hook.main`: `run` is itself a J1 entry symbol, and
    # `main` calls `log_invocation`, which would write a real row to the real
    # ledger as a side effect of measuring.
    "J1": "hook.run(repo, base, head, registry) — the pre-push consumer",
}


def _tracer(code_root):
    """`tgtrace`'s collector without its pytest hooks.

    Importable without pytest by construction (its module docstring says so, so
    `tests/test_ground_truth.py` can drive it), which is what makes reusing it
    here legitimate rather than a second copy of the same machinery. TGTRACE_ROOT
    must be set BEFORE the import: the module reads it at import time.

    `code_root` is where the PRODUCT CODE being executed lives — NOT `--repo`,
    which is the analysis target (its git history, its index, its registry).
    The first run of this file conflated them and measured zero: `--repo` was
    `testgraph/main` while the imported `testgraph` package came from the
    worktree this harness runs in, so `_relpath` rejected every frame as
    outside the root and the real half recorded nothing at all. The two halves
    stay comparable because both sides key on repo-RELATIVE paths
    (`testgraph/select.py`), which are identical across checkouts of the same
    layout."""
    os.environ["TGTRACE_ROOT"] = os.path.abspath(code_root)
    # The instrument must not appear in its own measurement. The tracer's
    # shutdown (`_stop`, `_stop_monitoring`) and this file's own `invoke`
    # wrapper execute inside the traced window by construction, and they landed
    # in `real_only` on the first honest run — padding the one number that
    # decides the phase with the thing doing the measuring. They can never
    # appear in the unit half, so excluding them makes the two sides MORE
    # comparable, not less.
    os.environ["TGTRACE_SKIP"] = ":".join((
        "/tests/", "/test_", "/.venv/",
        "harness/plugin/", "harness/unit_vs_journey.py",
    ))
    sys.path.insert(0, os.path.join(HERE, "plugin"))
    import tgtrace  # noqa: E402
    return tgtrace


def unit_symbols(conn, registry, trace, journey):
    """Symbols executed by the tests credited to `journey`, and how many.

    Attribution is against journey ENTRY symbols — the same rule
    `pytest_adapter.attribute` uses, and for the same reason a footprint is
    wrong for it (a footprint made one CLI's 34 tests cover every journey)."""
    files = rec.file_map(conn)
    per_test, _unresolved = rec.trace_nodes(conn, trace.get("tests", {}), files)
    entries = {
        nid for nid, jids in reg.resolve_entries(conn, registry).items()
        if journey in jids
    }
    covering = [t for t, ids in per_test.items() if ids & entries]
    symbols = set()
    for test, syms in trace.get("tests", {}).items():
        if test in covering:
            symbols.update(tuple(s) for s in syms)
    return symbols, covering


def real_symbols(tgtrace, fn):
    """Symbols executed by one real invocation.

    `_current` is the set the collector writes into; it is None outside a test
    body, which is exactly the window this opens by hand."""
    tgtrace._current = set()
    tgtrace._start()
    try:
        fn()
    finally:
        tgtrace._stop()
        symbols = tgtrace._current
        tgtrace._current = None
    return set(symbols)


def _named(symbols):
    """Drop anonymous frames — `<lambda>`, `<genexpr>`, `<listcomp>`.

    The index has no node for them, so they cannot be a missing-edge finding,
    and `reconcile.resolve_traced` drops them for the same reason. Applied to
    BOTH halves so the filter cannot shift the comparison in either
    direction."""
    return {(rel, qual) for rel, qual in symbols
            if "<" not in qual.split(".")[-1]}


def render(journey, unit, real, covering, error):
    lines = [
        f"{journey}  unit-vs-real  ({len(covering)} covering test(s))",
        f"  REAL invocation : {REAL_INVOCATIONS.get(journey, '?')}",
    ]
    if error:
        lines.append(f"  REAL FAILED: {error}")
        lines.append(
            "  NO MEASUREMENT — the real half did not run, so there is nothing "
            "to compare. An empty `real - unit` here would read exactly like a "
            "clean result."
        )
        return "\n".join(lines)
    if not real:
        # An empty real half makes `real - unit` empty too, and "REAL-ONLY:
        # none" is the sentence a reader quotes. It is indistinguishable from
        # the clean result. Same refusal `harness/ground_truth.py` and
        # `testgraph.reconcile` both make, and the one issue #75 fixed for a
        # NONE printed over a trust warning -- and the first run of this file
        # printed exactly that clean bill over a null measurement.
        lines.append(
            "  NO MEASUREMENT — the real invocation ran but recorded 0 symbols, "
            "so nothing was compared and there is no real-only count. Check "
            "that TGTRACE_ROOT covers the code actually being imported."
        )
        return "\n".join(lines)
    only_real = sorted(real - unit)
    only_unit = sorted(unit - real)
    lines.append(
        f"  unit {len(unit)} symbol(s)   real {len(real)}   "
        f"shared {len(unit & real)}"
    )
    lines.append(
        f"  real_only {len(only_real)}   unit_only {len(only_unit)}"
    )
    lines.append("")
    if only_real:
        lines.append(
            "  REAL-ONLY — product code the journey runs that no test credited "
            "to it touches. This is the number that decides the phase:"
        )
        for rel, qual in only_real:
            lines.append(f"    - {qual} ({rel})")
    else:
        lines.append(
            "  REAL-ONLY: none. Every symbol the real journey executed was "
            "already reached by a test credited to it."
        )
    lines.append("")
    lines.append(
        f"  unit-only is {len(only_unit)} and is EXPECTED — unit tests exercise "
        f"error paths and guards one real invocation does not reach."
    )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness/unit_vs_journey.py")
    ap.add_argument("--repo", required=True, help="a REAL repo to run against")
    ap.add_argument("--trace", required=True, help="tgtrace payload of the suite")
    ap.add_argument("--journey", default="J2")
    ap.add_argument("--db", default=None)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    registry_path = args.registry or reg.resolve_for_repo(args.repo)
    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    registry = reg.load(registry_path)
    conn = dbmod.connect(db_path)

    with open(args.trace) as fh:
        trace = json.load(fh)
    unit, covering = unit_symbols(conn, registry, trace, args.journey)
    unit = _named(unit)

    if args.journey not in REAL_INVOCATIONS:
        print(f"no real invocation defined for {args.journey} — add one to "
              f"REAL_INVOCATIONS rather than guessing")
        return 2

    tgtrace = _tracer(ROOT)

    def invoke():
        if args.journey == "J2":
            sel.select(args.repo, args.base, args.head, db_path, registry_path,
                       strict_registry=False)
        elif args.journey == "J1":
            hook.run(args.repo, args.base, args.head, registry_path)
        else:
            raise NotImplementedError(args.journey)

    error = None
    try:
        real = real_symbols(tgtrace, invoke)
    except Exception as exc:                       # noqa: BLE001 - reported
        real, error = set(), f"{type(exc).__name__}: {exc}"
    real = _named(real)

    if args.json:
        print(json.dumps({
            "journey": args.journey,
            "covering_tests": len(covering),
            "unit": sorted(map(list, unit)),
            "real": sorted(map(list, real)),
            "real_only": sorted(map(list, real - unit)),
            "unit_only": sorted(map(list, unit - real)),
            "error": error,
        }, indent=1, sort_keys=True))
    else:
        print(render(args.journey, unit, real, covering, error))
    return 0 if not error else 1


if __name__ == "__main__":
    sys.exit(main())
