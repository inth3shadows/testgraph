"""Unit tests for the closure + integrity guard, on a synthetic fixture db so
they run without codegraph. Covers S2 (guard blocks a corrupted index) and the
recall-critical closure behaviors (imports edge + file-expansion; leaf stays
tight)."""
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from testgraph import db as dbmod  # noqa: E402
from testgraph import integrity  # noqa: E402
from testgraph import select as sel  # noqa: E402


def build_fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT);
        CREATE TABLE unresolved_refs(id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE files(path TEXT, language TEXT, indexed_at INTEGER);
        """
    )
    nodes = [
        ("function:get_settings", "function", "get_settings", "get_settings",
         "app/config.py", 1, 5),
        ("file:app/svc.py", "file", "svc.py", "svc.py", "app/svc.py", 1, 999),
        ("function:handler_a", "function", "handler_a", "handler_a",
         "app/svc.py", 10, 20),
        ("function:leaf", "function", "leaf", "leaf", "app/leaf.py", 1, 5),
        # confidence fixture: base <- {mid_a weak, mid_b strong} <- top,
        # plus a synthesized (heuristic) caller.
        ("function:base", "function", "base", "base", "app/conf.py", 1, 5),
        ("function:mid_a", "function", "mid_a", "mid_a", "app/conf.py", 10, 15),
        ("function:mid_b", "function", "mid_b", "mid_b", "app/conf.py", 20, 25),
        ("function:top", "function", "top", "top", "app/conf.py", 30, 35),
        ("function:hcaller", "function", "hcaller", "hcaller", "app/conf.py", 40, 45),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", nodes)
    edges = [
        # module-level `_settings = get_settings()` -> recorded as an imports edge
        ("file:app/svc.py", "function:get_settings", "imports", None, None),
        # svc.py contains handler_a
        ("file:app/svc.py", "function:handler_a", "contains", None, None),
        # two routes from base up to top: a weak hop and a strong one
        ("function:mid_a", "function:base", "calls", '{"confidence":0.5}', None),
        ("function:mid_b", "function:base", "calls", '{"confidence":0.9}', None),
        ("function:top", "function:mid_a", "calls", '{"confidence":0.9}', None),
        ("function:top", "function:mid_b", "calls", '{"confidence":0.9}', None),
        # synthesized edge: capped regardless of the confidence it claims
        ("function:hcaller", "function:base", "calls", '{"confidence":0.9}', "heuristic"),
    ]
    conn.executemany(
        "INSERT INTO edges(source,target,kind,metadata,provenance) VALUES (?,?,?,?,?)",
        edges,
    )
    conn.executemany(
        "INSERT INTO files VALUES (?,?,?)",
        [("app/config.py", "python", int(time.time() * 1000) + 60000)],
    )
    conn.commit()
    return conn


class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture()

    def test_imports_and_file_expansion_reach_handler(self):
        # a change to get_settings must reach handler_a through the imports
        # edge + file expansion — the module-level-singleton recall path.
        impacted = dbmod.impacted_closure(self.conn, {"function:get_settings"})
        self.assertIn("function:handler_a", impacted)
        self.assertIn("file:app/svc.py", impacted)

    def test_leaf_stays_tight(self):
        # nothing imports/calls leaf -> closure is just itself (precision).
        impacted = dbmod.impacted_closure(self.conn, {"function:leaf"})
        self.assertEqual(set(impacted), {"function:leaf"})
        self.assertEqual(impacted["function:leaf"], 1.0)

    def test_missing_metadata_defaults_high(self):
        # the imports/contains edges carry no metadata -> must not be treated as
        # weak, or every shared-global journey would falsely demand manual review.
        impacted = dbmod.impacted_closure(self.conn, {"function:get_settings"})
        self.assertEqual(
            impacted["function:handler_a"], dbmod.DEFAULT_EDGE_CONFIDENCE
        )

    def test_line_mapping(self):
        seeds = dbmod.nodes_for_lines(self.conn, "app/svc.py", 12, 13)
        self.assertIn("function:handler_a", seeds)
        self.assertNotIn("function:leaf", seeds)


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.impacted = dbmod.impacted_closure(build_fixture(), {"function:base"})

    def test_min_along_path(self):
        # one 0.5 hop drags the whole route down to 0.5.
        self.assertEqual(self.impacted["function:mid_a"], 0.5)
        self.assertEqual(self.impacted["function:mid_b"], 0.9)

    def test_max_across_paths(self):
        # top is reachable via mid_a (min 0.5) and mid_b (min 0.9) -> 0.9 wins:
        # one trustworthy route is enough.
        self.assertEqual(self.impacted["function:top"], 0.9)

    def test_heuristic_edge_is_capped(self):
        # claims 0.9 in metadata, but provenance='heuristic' floors it.
        self.assertEqual(
            self.impacted["function:hcaller"], dbmod.HEURISTIC_CONFIDENCE
        )
        self.assertLessEqual(self.impacted["function:hcaller"], dbmod.LOW_CONFIDENCE)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture()

    def test_healthy_passes(self):
        spot = {"get_settings": {"min_caller_edges": 1, "file": "config.py"}}
        blocking, _ = integrity.check(self.conn, "/nonexistent", spot)
        self.assertEqual(blocking, [])

    def test_corrupt_index_blocks(self):
        # simulate the sync-didn't-repair corruption: too few caller edges.
        spot = {"get_settings": {"min_caller_edges": 10, "file": "config.py"}}
        blocking, _ = integrity.check(self.conn, "/nonexistent", spot)
        self.assertTrue(any("corrupt" in b for b in blocking))

    def test_missing_spot_symbol_blocks(self):
        spot = {"nonexistent_sym": {"min_caller_edges": 1}}
        blocking, _ = integrity.check(self.conn, "/nonexistent", spot)
        self.assertTrue(any("missing" in b for b in blocking))

    def test_pending_refs_block(self):
        self.conn.execute("INSERT INTO unresolved_refs(status) VALUES ('pending')")
        blocking, _ = integrity.check(self.conn, "/nonexistent", {})
        self.assertTrue(any("pending" in b for b in blocking))


class DiffParseTests(unittest.TestCase):
    def test_addition_seeds_range(self):
        diff = (
            "diff --git a/app/svc.py b/app/svc.py\n"
            "--- a/app/svc.py\n+++ b/app/svc.py\n"
            "@@ -10,0 +11,3 @@\n+x\n+y\n+z\n"
        )
        self.assertEqual(sel._parse_unified_diff(diff), {"app/svc.py": [(11, 13)]})

    def test_deletion_only_still_seeds(self):
        # +124,0 is a pure deletion — must STILL seed (recall-first regression).
        diff = "--- a/app/svc.py\n+++ b/app/svc.py\n@@ -125,3 +124,0 @@\n"
        self.assertEqual(sel._parse_unified_diff(diff), {"app/svc.py": [(124, 125)]})

    def test_deleted_file_not_seeded(self):
        diff = "--- a/app/svc.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
        self.assertEqual(sel._parse_unified_diff(diff), {})

    def test_test_files_excluded(self):
        diff = "+++ b/backend/tests/test_x.py\n@@ -1,0 +1,2 @@\n+a\n+b\n"
        self.assertEqual(sel._parse_unified_diff(diff), {})


if __name__ == "__main__":
    unittest.main()
