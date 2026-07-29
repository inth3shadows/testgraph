"""Unit tests for the closure + integrity guard, on a synthetic fixture db so
they run without codegraph. Covers S2 (guard blocks a corrupted index) and the
recall-critical closure behaviors (imports edge + file-expansion; leaf stays
tight)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from testgraph import db as dbmod  # noqa: E402
from testgraph import integrity  # noqa: E402
from testgraph import registry as reg  # noqa: E402
from testgraph import export as exp  # noqa: E402
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
        CREATE TABLE schema_versions(version INT, applied_at INT, note TEXT);
        INSERT INTO schema_versions VALUES (8, 0, 'fixture');
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

    def test_matching_schema_pin_passes(self):
        blocking, warnings = integrity.check(
            self.conn, "/nonexistent", {}, schema_pin=8
        )
        self.assertEqual(blocking, [])
        self.assertFalse(any("unpinned" in w for w in warnings))

    def test_schema_drift_blocks(self):
        # a codegraph upgrade that renames a column must fail loud, not return
        # a confidently-wrong narrow answer (plan risk R1).
        blocking, _ = integrity.check(self.conn, "/nonexistent", {}, schema_pin=9)
        self.assertTrue(any("schema 8 != pinned 9" in b for b in blocking))

    def test_pin_without_schema_row_blocks(self):
        self.conn.execute("DROP TABLE schema_versions")
        blocking, _ = integrity.check(self.conn, "/nonexistent", {}, schema_pin=8)
        self.assertTrue(any("no schema_versions row" in b for b in blocking))

    def test_unpinned_schema_warns(self):
        _, warnings = integrity.check(self.conn, "/nonexistent", {})
        self.assertTrue(any("unpinned" in w for w in warnings))


class DiffParseTests(unittest.TestCase):
    def test_addition_seeds_range(self):
        diff = (
            "diff --git a/app/svc.py b/app/svc.py\n"
            "--- a/app/svc.py\n+++ b/app/svc.py\n"
            "@@ -10,0 +11,3 @@\n+x\n+y\n+z\n"
        )
        self.assertEqual(
            sel._parse_unified_diff(diff), ({"app/svc.py": [(11, 13)]}, {})
        )

    def test_deletion_only_still_seeds(self):
        # +124,0 is a pure deletion — must STILL seed (recall-first regression).
        diff = "--- a/app/svc.py\n+++ b/app/svc.py\n@@ -125,3 +124,0 @@\n"
        self.assertEqual(
            sel._parse_unified_diff(diff), ({"app/svc.py": [(124, 125)]}, {})
        )

    def test_deleted_file_is_a_whole_file_change(self):
        # '+++ /dev/null' used to drop the file entirely — the surviving path is
        # on the '---' line, and its dependents still need selecting.
        diff = "--- a/app/svc.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
        ranges, whole = sel._parse_unified_diff(diff)
        self.assertEqual(ranges, {})
        self.assertEqual(whole, {"app/svc.py": "deleted"})

    def test_rename_with_no_content_change_is_caught(self):
        # a pure rename emits no '@@' hunks at all, but changes the module path
        # for every importer.
        diff = (
            "diff --git a/app/old.py b/app/new.py\n"
            "similarity index 100%\n"
            "rename from app/old.py\nrename to app/new.py\n"
        )
        ranges, whole = sel._parse_unified_diff(diff)
        self.assertEqual(ranges, {})
        self.assertEqual(whole, {"app/old.py": "renamed from", "app/new.py": "renamed to"})

    def test_deleted_test_file_still_ignored(self):
        diff = "--- a/app/tests/test_x.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
        self.assertEqual(sel._parse_unified_diff(diff), ({}, {}))

    def test_test_files_excluded(self):
        diff = "+++ b/backend/tests/test_x.py\n@@ -1,0 +1,2 @@\n+a\n+b\n"
        self.assertEqual(sel._parse_unified_diff(diff), ({}, {}))


class WholeFileSelectTests(unittest.TestCase):
    """End-to-end over a real git repo + a fixture index, covering the two
    deleted-file cases: the index still has the file (seeds resolve) and the
    index has already dropped it (impact unbounded -> degrade to all journeys)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "app"))
        run = lambda *a: subprocess.run(
            a, cwd=self.repo, check=True, capture_output=True
        )
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        with open(os.path.join(self.repo, "app", "gone.py"), "w") as fh:
            fh.write("def doomed():\n    return 1\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "add")
        os.remove(os.path.join(self.repo, "app", "gone.py"))
        run("git", "add", "-A")
        run("git", "commit", "-qm", "delete")

        self.registry = os.path.join(self.tmp, "reg.json")
        with open(self.registry, "w") as fh:
            json.dump(
                {
                    "codegraph_schema_version": 8,
                    "journeys": {
                        "J1": {
                            "name": "j one",
                            "entries": [{"name": "handler_a", "file": "app/svc.py"}],
                        },
                        "J2": {
                            "name": "j two",
                            "entries": [{"name": "leaf", "file": "app/leaf.py"}],
                        },
                    },
                    "spot_checks": {},
                },
                fh,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, with_deleted_file):
        path = os.path.join(self.tmp, "cg.db")
        if os.path.exists(path):
            os.remove(path)
        src = build_fixture()
        dst = sqlite3.connect(path)
        src.backup(dst)
        if with_deleted_file:
            # index predates the deletion: gone.py's symbol is still present and
            # handler_a imports it, so J1 must be selected.
            dst.execute(
                "INSERT INTO nodes VALUES "
                "('function:doomed','function','doomed','doomed','app/gone.py',1,2)"
            )
            dst.execute(
                "INSERT INTO edges(source,target,kind,metadata,provenance) VALUES "
                "('function:handler_a','function:doomed','calls','{\"confidence\":0.9}',NULL)"
            )
            dst.commit()
        dst.close()
        return path

    def test_deleted_file_in_index_seeds_its_dependents(self):
        res = sel.select(
            self.repo, "HEAD~1", "HEAD", self._db(True), self.registry
        )
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["whole_file_changes"], {"app/gone.py": "deleted"})
        self.assertFalse(res["recall_degraded"])
        self.assertIn("J1", [j["id"] for j in res["journeys"]])

    def test_deleted_file_absent_from_index_degrades_to_all_journeys(self):
        res = sel.select(
            self.repo, "HEAD~1", "HEAD", self._db(False), self.registry
        )
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["recall_degraded"])
        self.assertEqual({"J1", "J2"}, {j["id"] for j in res["journeys"]})
        self.assertTrue(all(j["verify_manually"] for j in res["journeys"]))
        self.assertTrue(any("unbounded" in w for w in res["warnings"]))


class ExportMapTests(unittest.TestCase):
    """The map is the selector's reverse index. If it disagrees with select() it
    is worse than absent, because an agent would trust it."""

    REG = {
        "journeys": {
            "J1": {"name": "one", "entries": [{"name": "handler_a",
                                               "file": "app/svc.py"}]},
            "J2": {"name": "two", "entries": [{"name": "leaf",
                                               "file": "app/leaf.py"}]},
        },
        "spot_checks": {},
    }

    def setUp(self):
        self.conn = build_fixture()
        self.rows = exp.build_map(self.conn, self.REG)

    def _row(self, path, symbol):
        return next(r for r in self.rows[path] if r["symbol"] == symbol)

    def test_entry_symbol_maps_to_its_own_journey(self):
        self.assertEqual(self._row("app/svc.py", "handler_a")["journeys"], ["J1"])

    def test_shared_global_fans_out_through_imports(self):
        # get_settings reaches handler_a via the imports edge + file expansion,
        # the recall path the closure exists for.
        self.assertEqual(
            self._row("app/config.py", "get_settings")["journeys"], ["J1"]
        )

    def test_symbol_reaching_nothing_is_omitted(self):
        # mid_a/top are in no journey's closure -> absent, which the skill reads
        # as "no journeys affected".
        present = {r["symbol"] for rows in self.rows.values() for r in rows}
        self.assertNotIn("top", present)

    def test_map_agrees_with_closure_for_every_row(self):
        entry_map = reg.resolve_entries(self.conn, self.REG)
        for path, rows in self.rows.items():
            for r in rows:
                nid = dbmod.nodes_for_lines(self.conn, path, *r["lines"])
                closure = dbmod.impacted_closure(self.conn, set(nid))
                reachable = {jid for e, jid in entry_map.items() if e in closure}
                # line-overlap can pull in neighbouring symbols, so the row is a
                # subset of what a diff on those lines would select -- never more.
                self.assertTrue(
                    set(r["journeys"]) <= reachable,
                    f"{path}:{r['symbol']} map={r['journeys']} > select={reachable}",
                )


class IntoTargetTests(unittest.TestCase):
    """`--into-target` is what makes the skill's in-repo lookup work, so its path
    construction and its refusal to litter a blocked target are both load-bearing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, ".codegraph"))
        db = os.path.join(self.tmp, ".codegraph", "codegraph.db")
        src = build_fixture()
        dst = sqlite3.connect(db)
        src.backup(dst)
        dst.close()
        self.reg = os.path.join(self.tmp, "reg.json")
        with open(self.reg, "w") as fh:
            json.dump(
                {
                    "codegraph_schema_version": 8,
                    "journeys": {"J1": {"name": "one", "entries": [
                        {"name": "handler_a", "file": "app/svc.py"}]}},
                    "spot_checks": {},
                },
                fh,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_where_the_skill_looks(self):
        rc = exp.main(["--repo", self.tmp, "--registry", self.reg, "--into-target"])
        self.assertEqual(rc, 0)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, ".testgraph", "journey-map.md"))
        )

    def test_out_and_into_target_are_exclusive(self):
        rc = exp.main(["--repo", self.tmp, "--registry", self.reg, "--into-target",
                       "--out", os.path.join(self.tmp, "x.md")])
        self.assertEqual(rc, 2)

    def test_blocked_run_leaves_no_directory_behind(self):
        # a run that says "map NOT written" must not litter the target repo.
        with open(self.reg) as fh:
            reg = json.load(fh)
        reg["codegraph_schema_version"] = 99
        with open(self.reg, "w") as fh:
            json.dump(reg, fh)
        rc = exp.main(["--repo", self.tmp, "--registry", self.reg, "--into-target"])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".testgraph")))


if __name__ == "__main__":
    unittest.main()
