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


def build_fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT);
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
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", nodes)
    edges = [
        # module-level `_settings = get_settings()` -> recorded as an imports edge
        ("file:app/svc.py", "function:get_settings", "imports"),
        # svc.py contains handler_a
        ("file:app/svc.py", "function:handler_a", "contains"),
    ]
    conn.executemany(
        "INSERT INTO edges(source,target,kind) VALUES (?,?,?)", edges
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
        self.assertEqual(impacted, {"function:leaf"})

    def test_line_mapping(self):
        seeds = dbmod.nodes_for_lines(self.conn, "app/svc.py", 12, 13)
        self.assertIn("function:handler_a", seeds)
        self.assertNotIn("function:leaf", seeds)


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


if __name__ == "__main__":
    unittest.main()
