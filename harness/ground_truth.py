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


def journey_traces(trace, test_map):
    """{journey_id: {(relpath, qualname), ...}} plus the test ids that had no mapping.

    A test maps to a journey by exact nodeid, else by its FILE. File-level is the
    normal case (`tests/test_auth.py` -> J6) and nodeid-level exists for the files
    that straddle journeys, which is most of what makes this map hand-authored
    rather than derived."""
    by_journey = collections.defaultdict(set)
    unmapped = []
    for nodeid, symbols in trace["tests"].items():
        path = nodeid.split("::", 1)[0]
        jids = test_map.get(nodeid) or test_map.get(path) or test_map.get(
            os.path.basename(path)
        )
        if not jids:
            unmapped.append(nodeid)
            continue
        for jid in jids:
            by_journey[jid].update(tuple(s) for s in symbols)
    return by_journey, unmapped


def resolve_traced(conn, symbols):
    """({node_id, ...}, [(relpath, qualname), ...]) — resolved ids and the misses.

    A traced qualname is `Class.method` or `outer.<locals>.inner`; the index
    stores the bare symbol name, so match on the last dotted component and
    constrain by file path. `<locals>` frames are dropped: a closure defined
    inside a function is not a node the selector could ever seed on, so counting
    it as a miss would inflate the number with something no edge kind fixes."""
    resolved, unresolved = set(), []
    for rel, qualname in symbols:
        if "<locals>" in qualname or "<" in qualname.split(".")[-1]:
            continue
        name = qualname.split(".")[-1]
        ids = dbmod.resolve_symbol(conn, name, os.path.basename(rel))
        # Narrow to the traced file when several files define the same name.
        exact = [i for i in ids if _file_of(conn, i, rel)]
        hits = exact or ids
        if hits:
            resolved.update(hits)
        else:
            unresolved.append((rel, qualname))
    return resolved, unresolved


_FILE_CACHE = {}


def _file_of(conn, node_id, rel):
    if node_id not in _FILE_CACHE:
        row = conn.execute("SELECT file_path FROM nodes WHERE id = ?", (node_id,)).fetchone()
        _FILE_CACHE[node_id] = row[0] if row else ""
    return (_FILE_CACHE[node_id] or "").endswith(rel)


def compare(conn, registry, by_journey):
    """Per-journey traced-vs-static comparison. Journeys with no trace are kept,
    flagged `no_trace` — a journey the suite does not cover is a hole in the
    MEASUREMENT, and dropping it silently would make coverage look like agreement."""
    reach, contained_by = couple.load_graph(conn)
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
        traced_ids, unresolved = resolve_traced(conn, traced_syms)
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
    misses = sum(len(r["traced_only"]) for r in covered)
    lines.append(
        f"{len(covered)}/{len(rows)} journey(s) traced; "
        f"{misses} traced symbol(s) outside the static footprint"
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
        flag = "  ! SILENT-MISS SOURCE" if r["traced_only"] else ""
        lines.append(
            f"  {r['journey']}  {r['name']}{flag}\n"
            f"      traced {r['traced_symbols']} symbol(s) -> {r['traced_nodes']} node(s); "
            f"static footprint {r['static_nodes']}\n"
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
