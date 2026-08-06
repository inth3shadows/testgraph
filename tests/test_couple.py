"""`harness/couple.py` inverts `db.impacted_closure`. That inversion is the whole
basis of the arm split, and it is the kind of thing that reads correct and is not
— so it is checked here against the real closure rather than by eye.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.couple import build_registry, footprint, load_graph  # noqa: E402
from testgraph import db as dbmod  # noqa: E402


def build_graph():
    """A graph exercising every rule the inversion has to mirror: a plain call
    chain, a file-level `imports` edge (the module-singleton shape that made
    `imports` load-bearing for recall), file containment, and a CLASS `contains`
    edge that must NOT be walked."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT);
        """
    )
    nodes = [
        ("file:app/a.py", "file", "a.py", "a.py", "app/a.py", 1, 999),
        ("function:h1", "function", "h1", "h1", "app/a.py", 10, 20),
        ("function:h2", "function", "h2", "h2", "app/a.py", 30, 40),
        ("function:shared", "function", "shared", "shared", "app/core.py", 1, 9),
        ("function:leaf", "function", "leaf", "leaf", "app/leaf.py", 1, 9),
        ("function:cfg", "function", "cfg", "cfg", "app/config.py", 1, 9),
        ("class:C", "class", "C", "C", "app/k.py", 1, 99),
        ("function:m", "function", "m", "m", "app/k.py", 5, 9),
        ("function:orphan", "function", "orphan", "orphan", "app/o.py", 1, 9),
    ]
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", nodes
    )
    edges = [
        ("file:app/a.py", "function:h1", "contains"),
        ("file:app/a.py", "function:h2", "contains"),
        ("function:h1", "function:shared", "calls"),
        ("function:shared", "function:leaf", "calls"),
        # module-level singleton: recorded file -> symbol, never as a call
        ("file:app/a.py", "function:cfg", "imports"),
        # class containment must not participate — only `file:` sources do
        ("class:C", "function:m", "contains"),
    ]
    conn.executemany(
        "INSERT INTO edges(source, target, kind, metadata, provenance) "
        "VALUES (?,?,?,NULL,NULL)",
        edges,
    )
    return conn


class FootprintInvertsClosureTest(unittest.TestCase):
    """The property that matters: `X in footprint(E)` must mean exactly
    `E in impacted_closure({X})`, for every node in the graph. Both directions —
    a missed node would put a journey in the wrong arm, and a spurious one would
    inflate the shared core."""

    def test_agrees_with_impacted_closure_for_every_node(self):
        conn = build_graph()
        reach, contained_by = load_graph(conn)
        all_ids = [r[0] for r in conn.execute("SELECT id FROM nodes")]
        for entry in all_ids:
            dep = footprint([entry], reach, contained_by)
            for x in all_ids:
                selects = entry in dbmod.impacted_closure(conn, [x])
                self.assertEqual(
                    x in dep,
                    selects,
                    f"footprint({entry}) and impacted_closure({x}) disagree: "
                    f"in-footprint={x in dep}, selects={selects}",
                )

    def test_walks_the_import_edge_a_call_graph_would_miss(self):
        """`imports` is why the closure catches module-level singletons. If the
        inversion dropped it, every journey reading shared config would look
        isolated and land in the LOW arm."""
        conn = build_graph()
        reach, contained_by = load_graph(conn)
        self.assertIn("function:cfg", footprint(["function:h1"], reach, contained_by))

    def test_does_not_walk_class_containment(self):
        """Only `file:` sources satisfy `impacted_closure`'s rule 2. Walking a
        class's `contains` edge would drag a whole class into every method's
        footprint by structure alone, inflating coupling that is not there."""
        conn = build_graph()
        reach, contained_by = load_graph(conn)
        self.assertNotIn("class:C", footprint(["function:m"], reach, contained_by))

    def test_orphan_footprint_is_just_itself(self):
        conn = build_graph()
        reach, contained_by = load_graph(conn)
        self.assertEqual(
            footprint(["function:orphan"], reach, contained_by), {"function:orphan"}
        )


class BuildRegistryTest(unittest.TestCase):
    def test_renumbers_and_keeps_provenance(self):
        draft = {
            "codegraph_schema_version": 8,
            "spot_checks": {"GUID": {"min_edges": 151}},
            "spot_check_basis": "fan-in",
            "journeys": {
                "J_x": {"name": "GET /x", "entries": [{"name": "x", "file": "a.py"}],
                        "route": ["GET /x"]},
                "J_y": {"name": "GET /y", "entries": [{"name": "y", "file": "b.py"}]},
            },
        }
        reg = build_registry(draft, ["J_y", "J_x"], "demo", "note")
        self.assertEqual(list(reg["journeys"]), ["J1", "J2"])
        # order follows the jids passed in, not the draft's order
        self.assertEqual(reg["journeys"]["J1"]["drafted_as"], "J_y")
        self.assertEqual(reg["journeys"]["J2"]["route"], ["GET /x"])
        self.assertNotIn("route", reg["journeys"]["J1"])
        # provenance the integrity check needs must survive the split
        self.assertEqual(reg["codegraph_schema_version"], 8)
        self.assertEqual(reg["spot_checks"], {"GUID": {"min_edges": 151}})
        # a machine-derived measurement registry is never approved
        self.assertIs(reg["approved"], False)


if __name__ == "__main__":
    unittest.main()
