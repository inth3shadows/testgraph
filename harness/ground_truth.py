"""Score testgraph's static footprint against what a journey ACTUALLY executes.

The measurement half of issue #12. `harness/trace.py` produces the dynamic side;
this joins it to the static side and reports the disagreement.

The question
------------
For journey J with entry symbols E, `couple.footprint(E)` is `Dep(E)` — every
symbol whose change would put E in the impacted closure, i.e. exactly the set of
edits that would cause testgraph to select J. A trace of J gives the symbols J
really runs. So:

    traced_only = traced(J) - Dep(E)

is a symbol that J executes and that testgraph would NOT select J for. Edit one
of those and the selector stays silent about a journey the change can break —
the single failure mode a recall-first selector must not have. That number is the
point of this file; everything else is context for reading it.

    static_only = Dep(E) - traced(J)

is over-approximation, and is reported without being gated. Over-selection is the
direction this project chose on purpose, and a suite that does not cover a branch
also produces `static_only` entries that are not wrong at all — which is the
honest reason this side cannot be a precision score.

    unresolved  — traced symbols with no node in the index

is kept as its own bucket rather than folded into `traced_only`. "The graph has
no node for this" and "the graph has a node and no path to it" are different
defects with different fixes (index coverage vs missing edge kinds), and merging
them would report an indexing gap as a traversal gap.

Usage:
    python3 harness/ground_truth.py --trace traces/honeyslate.json \
        --map harness/journey_tests_honeyslate.json \
        --registry journeys/honeyslate.json \
        --db ~/personal_projects/honeyslate/main/.codegraph/codegraph.db
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import couple  # noqa: E402
from testgraph import db as dbmod  # noqa: E402
from testgraph import registry as reg  # noqa: E402


def lookup(test_map, nodeid):
    """The journeys a test nodeid maps to, or None.

    Tried longest-prefix-stripped-first: the exact nodeid, then the nodeid with
    leading path components removed, then the same for the file path alone.

    That stripping is not convenience, it is a correctness fix. pytest nodeids
    are relative to its *rootdir*, which for a target with no ini file falls back
    to the invocation directory — so `--repo <r>/backend --tests tests` yields
    `tests/test_auth.py::…` while `--repo <r> --tests backend/tests` yields
    `backend/tests/test_auth.py::…` for the very same test. A map keyed on one
    form silently matches nothing under the other, every journey reports
    `no_trace`, and the run looks clean. Matching on suffixes makes the map
    independent of how the harness happened to be invoked."""
    for candidate in (nodeid, nodeid.split("::", 1)[0]):
        parts = candidate.split("/")
        for i in range(len(parts)):
            key = "/".join(parts[i:])
            if key in test_map:
                return test_map[key]
    return None


def journey_traces(trace, test_map):
    """{journey_id: {(relpath, qualname), ...}} plus the test ids that had no mapping.

    A test maps to a journey by exact nodeid, else by its FILE. File-level is the
    normal case (`tests/test_auth.py` -> J6) and nodeid-level exists for the files
    that straddle journeys, which is most of what makes this map hand-authored
    rather than derived."""
    by_journey = collections.defaultdict(set)
    unmapped = []
    for nodeid, symbols in trace["tests"].items():
        jids = lookup(test_map, nodeid)
        if not jids:
            unmapped.append(nodeid)
            continue
        for jid in jids:
            by_journey[jid].update(tuple(s) for s in symbols)
    return by_journey, unmapped


def file_map(conn):
    """{node_id: file_path} for the whole index.

    Read once per connection and passed down rather than memoised in a module
    global. A global keyed on node id alone returns one database's answer for
    another's identically-named node, which is a wrong file narrowing with no
    symptom — and the only reason it never bit was that nothing yet compared two
    targets in one process."""
    return {
        row[0]: (row[1] or "")
        for row in conn.execute("SELECT id, file_path FROM nodes")
    }


def path_matches(indexed, rel):
    """Whether an index `file_path` denotes the traced relative path `rel`.

    A suffix match on whole path COMPONENTS. The tolerance is deliberate: the
    trace's relpath is taken against the TRACED root while `file_path` is
    relative to the INDEXED root, so when the index is rooted at a PARENT of the
    traced root, `file_path` is longer and `vendor/app/dyn.py` must still match a
    traced `app/dyn.py`.

    The opposite direction is NOT handled and cannot be, here: if the index is
    rooted BELOW the traced root, `file_path` is shorter than `rel` and no suffix
    match exists, so every symbol in that journey silently resolves to nothing
    and `traced_nodes` reads 0. That is indistinguishable from "the suite does
    not exercise this journey". Point `--db` at an index rooted at or above the
    traced root; `resolve_traced` reports the symbols as unresolved, and
    `render` surfaces a nonzero `unresolved` count, but neither can name the
    cause.

    What it must NOT do is match `myapp/dyn.py`, which a bare
    `indexed.endswith(rel)` does, because the boundary between components is not
    checked. Requiring the character before the suffix to be `/` is the whole
    difference between "the same file reached by a longer root" and "a different
    file whose directory happens to end in the right letters"."""
    return indexed == rel or indexed.endswith("/" + rel)


def resolve_traced(conn, symbols, files=None):
    """({node_id, ...}, [(relpath, qualname), ...]) — resolved ids and the misses.

    A traced qualname is `Class.method` or `outer.<locals>.inner`; the index
    stores the bare symbol name, so match on the last dotted component and
    constrain by file path. `<locals>` frames are dropped: a closure defined
    inside a function is not a node the selector could ever seed on, so counting
    it as a miss would inflate the number with something no edge kind fixes.

    A name that resolves only in a DIFFERENT file is reported as unresolved, not
    accepted. `resolve_symbol` matches on a basename `LIKE`, so a traced
    `app/dyn.py:audit` the index genuinely lacks would otherwise bind to
    `vendor/dyn.py:audit` — and whether it then counts as a miss would be decided
    by an unrelated node's edges. That is the indexing-gap/traversal-gap
    conflation this module refuses everywhere else.

    The comparison is `path_matches`, not a bare `endswith`, because a raw suffix
    test has no path boundary: `myapp/dyn.py`.endswith(`app/dyn.py`) is True, so
    the narrowing this docstring promises leaked at exactly the case it exists to
    stop — binding a traced symbol to a same-named node in an unrelated
    directory, silently."""
    files = files if files is not None else file_map(conn)
    resolved, unresolved = set(), []
    for rel, qualname in symbols:
        if "<locals>" in qualname or "<" in qualname.split(".")[-1]:
            continue
        name = qualname.split(".")[-1]
        ids = dbmod.resolve_symbol(conn, name, os.path.basename(rel))
        hits = [i for i in ids if path_matches(files.get(i, ""), rel)]
        if hits:
            resolved.update(hits)
        else:
            unresolved.append((rel, qualname))
    return resolved, unresolved


def compare(conn, registry, by_journey):
    """Per-journey traced-vs-static comparison. Journeys with no trace are kept,
    flagged `no_trace` — a journey the suite does not cover is a hole in the
    MEASUREMENT, and dropping it silently would make coverage look like agreement."""
    reach, contained_by = couple.load_graph(conn)
    files = file_map(conn)
    rows = []
    for jid in sorted(registry.get("journeys", {}), key=reg.journey_sort_key):
        journey = registry["journeys"][jid]
        entry_ids = []
        for entry in journey.get("entries", []):
            entry_ids.extend(
                dbmod.resolve_symbol(conn, entry["name"], entry.get("file"))
            )
        static = couple.footprint(entry_ids, reach, contained_by) if entry_ids else set()
        traced_syms = by_journey.get(jid, set())
        traced_ids, unresolved = resolve_traced(conn, traced_syms, files)
        rows.append(
            {
                "journey": jid,
                "name": journey.get("name", ""),
                "no_trace": not traced_syms,
                "entries_resolved": len(entry_ids),
                "traced_symbols": len(traced_syms),
                "traced_nodes": len(traced_ids),
                "static_nodes": len(static),
                "traced_only": sorted(traced_ids - static),
                "static_only": len(static - traced_ids),
                "unresolved": sorted(unresolved),
            }
        )
    return rows


def label(conn, node_id):
    row = conn.execute(
        "SELECT name, file_path FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    return f"{row[0]} ({row[1]})" if row else node_id


def render(rows, conn, unmapped, max_examples=8):
    lines = []
    covered = [r for r in rows if not r["no_trace"]]
    if not covered:
        # "0 journeys traced; 0 misses" is the sentence a reader quotes, and it
        # is indistinguishable from a clean result. A measurement that measured
        # nothing must not have a miss count at all.
        lines.append(
            f"NO MEASUREMENT — 0/{len(rows)} journey(s) traced. Nothing was "
            f"compared, so there is no miss count. "
            + (
                f"All {len(unmapped)} traced test(s) map to no journey: check "
                f"the map's keys against the nodeids in the trace."
                if unmapped
                else "The trace contains no tests."
            )
        )
        return "\n".join(lines)
    scored = [r for r in covered if r["entries_resolved"]]
    misses = sum(len(r["traced_only"]) for r in scored)
    if not scored:
        lines.append(
            f"NO MEASUREMENT — {len(covered)} journey(s) traced but none had a "
            f"resolvable entry symbol, so no footprint was compared. There is no "
            f"miss count. Check the registry against this --db."
        )
    else:
        lines.append(
            f"{len(scored)}/{len(rows)} journey(s) scored; "
            f"{misses} traced symbol(s) outside the static footprint"
        )
    if len(covered) > len(scored):
        lines.append(
            f"  {len(covered) - len(scored)} traced journey(s) NOT scored — "
            f"entry symbols did not resolve"
        )
    if unmapped:
        lines.append(
            f"  {len(unmapped)} traced test(s) map to no journey — "
            f"e.g. {', '.join(unmapped[:3])}"
        )
    lines.append("")
    for r in rows:
        if r["no_trace"]:
            lines.append(
                f"  {r['journey']}  {r['name']}  — NO TRACE "
                f"(the suite does not cover this journey; not evidence of agreement)"
            )
            continue
        if not r["entries_resolved"]:
            # Every traced node is "outside the footprint" when the footprint is
            # empty because the entry symbols did not resolve. Reporting that as
            # the selector's worst defect hides a renamed entry point or a
            # mismatched --db behind a wall of false silent misses.
            lines.append(
                f"  {r['journey']}  {r['name']}  — ENTRIES UNRESOLVED "
                f"(no entry symbol found in the index; the footprint is empty for "
                f"that reason, not because the selector missed anything. Check the "
                f"registry against this --db.)"
            )
            continue
        flag = "  ! SILENT-MISS SOURCE" if r["traced_only"] else ""
        lines.append(
            f"  {r['journey']}  {r['name']}{flag}\n"
            f"      traced {r['traced_symbols']} symbol(s) -> {r['traced_nodes']} node(s); "
            f"static footprint {r['static_nodes']} "
            f"(from {r['entries_resolved']} resolved entry symbol(s))\n"
            f"      traced_only {len(r['traced_only'])}   "
            f"static_only {r['static_only']}   unresolved {len(r['unresolved'])}"
        )
        for nid in r["traced_only"][:max_examples]:
            lines.append(f"        - {label(conn, nid)}")
        if len(r["traced_only"]) > max_examples:
            lines.append(f"        … {len(r['traced_only']) - max_examples} more")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness/ground_truth.py")
    ap.add_argument("--trace", required=True)
    ap.add_argument("--map", required=True, dest="test_map")
    ap.add_argument("--registry", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    with open(args.trace) as f:
        trace = json.load(f)
    with open(args.test_map) as f:
        test_map = json.load(f)
    registry = reg.load(args.registry)
    conn = dbmod.connect(os.path.expanduser(args.db))

    by_journey, unmapped = journey_traces(trace, test_map.get("tests", test_map))
    rows = compare(conn, registry, by_journey)

    if args.json:
        print(json.dumps({"unmapped": unmapped, "journeys": rows}, indent=1, sort_keys=True))
    else:
        print(render(rows, conn, unmapped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
