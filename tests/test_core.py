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

    def test_freshness_covers_non_python_sources(self):
        # issue #31: the query filtered `language = 'python'`, so a frontend file
        # edited after the last index gave a narrow answer with no staleness
        # warning — harmless before non-Python paths were seeded, wrong after.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "web"))
        edited = os.path.join(tmp, "web", "App.svelte")
        with open(edited, "w") as fh:
            fh.write("<script>let x = 1</script>\n")
        # indexed an hour before the file was written
        self.conn.execute(
            "INSERT INTO files VALUES (?,?,?)",
            ("web/App.svelte", "svelte", int((time.time() - 3600) * 1000)),
        )
        _, warnings = integrity.check(self.conn, tmp, {})
        self.assertTrue(
            any("newer than the index" in w and "App.svelte" in w for w in warnings),
            f"no staleness warning for a non-Python source: {warnings}",
        )


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

    def test_non_python_product_paths_are_seeded(self):
        # issue #21's actual behavior change had no test: reverting _is_product to
        # `.py`-only kept every other test green. One case per indexed family.
        for path in (
            "web/src/App.svelte",
            "web/src/routes/page.tsx",
            "web/src/lib/api.ts",
            "web/src/legacy/util.cjs",
            "web/src/Widget.vue",
        ):
            with self.subTest(path=path):
                diff = f"+++ b/{path}\n@@ -1,0 +1,2 @@\n+a\n+b\n"
                self.assertEqual(
                    sel._parse_unified_diff(diff), ({path: [(1, 2)]}, {}),
                    f"{path} is product code the index covers but was dropped",
                )

    def test_js_test_conventions_excluded(self):
        # now that non-Python paths are seeded, their test files must be ignored
        # like Python's — including a repo-root `__tests__/`, which the old
        # substring match on "/__tests__/" missed entirely (issue #33).
        for path in (
            "web/src/api.test.ts",
            "web/src/api.spec.js",
            "web/src/__tests__/App.jsx",
            "__tests__/App.jsx",
            "tests/e2e_flow.ts",
        ):
            with self.subTest(path=path):
                diff = f"+++ b/{path}\n@@ -1,0 +1,2 @@\n+a\n+b\n"
                self.assertEqual(
                    sel._parse_unified_diff(diff), ({}, {}),
                    f"{path} is test code but was seeded as product code",
                )

    def test_type_declarations_are_not_product(self):
        # `.d.ts` has no runtime behavior and no nodes; admitting it would trip
        # the #29 zero-seed degrade and answer "test everything" for a types-only
        # commit (issue #33).
        diff = "+++ b/web/src/types/api.d.ts\n@@ -1,0 +1,2 @@\n+a\n+b\n"
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

    # The map-vs-selector invariant lives in MapAgreesWithSelectorTests below.
    # Two earlier attempts at it in this class could not fail: both recomputed
    # `impacted_closure` from the row's own line range, which always contains the
    # row's own node, so the closure was seeded with precisely the set
    # `build_map` had used and equality held by construction (#22, then #32).

    def test_weak_path_is_flagged_verify_manually(self):
        """confidence/verify_manually is the map's only safety signal and had no
        test: the fixture's 0.5 edge sat under `base`, which no entry depended on.
        Anchor a journey on `mid_a`, reached from `base` only via that 0.5 hop."""
        weak_reg = {
            "journeys": {"JW": {"name": "weak", "entries": [
                {"name": "mid_a", "file": "app/conf.py"}]}},
            "spot_checks": {},
        }
        rows = exp.build_map(self.conn, weak_reg)
        base_row = next(r for r in rows["app/conf.py"] if r["symbol"] == "base")
        self.assertEqual(base_row["confidence"]["JW"], 0.5)
        self.assertEqual(base_row["verify_manually"], ["JW"])
        # and the entry itself is reached at full confidence, so NOT flagged
        own = next(r for r in rows["app/conf.py"] if r["symbol"] == "mid_a")
        self.assertEqual(own["verify_manually"], [])


class UnresolvedJourneyTests(unittest.TestCase):
    """A journey whose entries do not resolve can never be selected. It must fail
    loud, not vanish while the registry still advertises it (issue #19)."""

    BAD = {
        "journeys": {
            "J1": {"name": "one", "entries": [{"name": "handler_a",
                                               "file": "app/svc.py"}]},
            "J9": {"name": "ghost", "entries": [{"name": "long_gone",
                                                 "file": "app/svc.py"}]},
        },
        "spot_checks": {},
    }

    def setUp(self):
        self.conn = build_fixture()

    def test_unresolved_is_detected(self):
        self.assertEqual(reg.unresolved(self.conn, self.BAD), [("J9", ["long_gone"])])

    def test_partially_resolvable_journey_is_not_flagged(self):
        # one live entry is enough -- the journey is still selectable.
        spec = {"journeys": {"J1": {"name": "one", "entries": [
            {"name": "handler_a", "file": "app/svc.py"},
            {"name": "long_gone", "file": "app/svc.py"}]}}, "spot_checks": {}}
        self.assertEqual(reg.unresolved(self.conn, spec), [])


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


def _git_repo(tmp, files):
    """A real git repo with `files` = {relpath: line_count}, one commit."""
    repo = os.path.join(tmp, "repo")
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    for rel, n in files.items():
        d = os.path.dirname(os.path.join(repo, rel))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(repo, rel), "w") as fh:
            fh.write("".join(f"line_{i} = {i}\n" for i in range(1, n + 1)))
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    return repo, run


def _db_on_disk(tmp, conn, name="cg.db"):
    path = os.path.join(tmp, name)
    dst = sqlite3.connect(path)
    conn.backup(dst)
    dst.close()
    return path


def _registry_file(tmp, journeys, name="reg.json"):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        json.dump(
            {"codegraph_schema_version": 8, "journeys": journeys, "spot_checks": {}},
            fh,
        )
    return path


class MapAgreesWithSelectorTests(unittest.TestCase):
    """The invariant the whole design rests on: for a given change, the map and
    the selector name the same journeys. A map that disagrees is worse than no
    map, because an agent trusts it — and they DID disagree in #21, where the map
    listed frontend files against a journey while `select` answered NONE.

    Both earlier attempts at this test recomputed the closure from the row's own
    line range, i.e. from the seed set `build_map` had already used, so equality
    was structural (#22, #32). Here the seed is derived the way the other tool
    derives it: a real git commit, parsed by `_parse_unified_diff` and mapped
    through `nodes_for_lines`, with the extension filter in the path. Reverting
    `_is_product` to `.py`-only fails the `.svelte` row.
    """

    # svc.py: handler_a 10-20 | config.py: get_settings 1-5 | leaf.py: leaf 1-5
    # conf.py: base 1-5 reaches mid_a 10-15 only over the fixture's 0.5 edge
    # J3 puts an entry in a .svelte file, so the comparison crosses the extension
    # filter; J4 anchors on mid_a, so it crosses the weak-edge axis — a map that
    # dropped low-confidence journeys would under-report and disagree.
    FILES = {"app/config.py": 6, "app/svc.py": 22, "app/leaf.py": 6,
             "app/conf.py": 20, "app/ui/App.svelte": 12}
    REG = {
        "J1": {"name": "one", "entries": [{"name": "handler_a",
                                           "file": "app/svc.py"}]},
        "J2": {"name": "two", "entries": [{"name": "leaf", "file": "app/leaf.py"}]},
        "J3": {"name": "three", "entries": [{"name": "mount",
                                             "file": "app/ui/App.svelte"}]},
        "J4": {"name": "four", "entries": [{"name": "mid_a",
                                            "file": "app/conf.py"}]},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = build_fixture()
        self.conn.execute(
            "INSERT INTO nodes VALUES ('function:mount','function','mount','mount',"
            "'app/ui/App.svelte',1,10)"
        )
        self.conn.commit()
        self.db = _db_on_disk(self.tmp, self.conn)
        self.registry = _registry_file(self.tmp, self.REG)
        self.repo, self.run = _git_repo(self.tmp, self.FILES)

    def _edit_line(self, path, lineno, marker):
        full = os.path.join(self.repo, path)
        with open(full) as fh:
            lines = fh.readlines()
        lines[lineno - 1] = f"changed_{marker} = {marker}\n"
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", f"edit {path}:{lineno}")

    def test_every_map_row_matches_what_select_answers_for_that_change(self):
        rows = exp.build_map(self.conn, {"journeys": self.REG, "spot_checks": {}})
        checked = 0
        for i, (path, row) in enumerate(
            (p, r) for p, rs in sorted(rows.items()) for r in rs
        ):
            lo, hi = row["lines"]
            self._edit_line(path, lo, i)
            res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
            self.assertEqual(res["status"], "OK", res.get("blocking"))
            self.assertFalse(
                res["recall_degraded"],
                f"{path}:{row['symbol']} — select could not map the change at all",
            )
            self.assertEqual(
                set(row["journeys"]), {j["id"] for j in res["journeys"]},
                f"{path}:{row['symbol']} map={row['journeys']} != "
                f"select={[j['id'] for j in res['journeys']]}",
            )
            # the safety signal must agree too: a map that quietly dropped weak
            # paths, or failed to flag one, would send an agent away reassured.
            self.assertEqual(
                set(row["verify_manually"]),
                {j["id"] for j in res["journeys"] if j["verify_manually"]},
                f"{path}:{row['symbol']} verify_manually disagrees with select",
            )
            checked += 1
        self.assertGreaterEqual(checked, 5, "map lost rows — comparison is thin")
        self.assertTrue(
            any(r["verify_manually"] for rs in rows.values() for r in rs),
            "no weak-edge row exercised, so under-reporting would go undetected",
        )
        self.assertIn(
            "app/ui/App.svelte", rows,
            "no non-Python row exercised, so the #21 disagreement is untested",
        )


class ZeroSeedDegradesTests(unittest.TestCase):
    """Issue #29: a changed file whose hunks resolve to no node used to produce
    `journeys to test: NONE` with `recall_degraded: False` and no warning — the
    exact confident-silence #21 exists to kill, on the line-range path instead of
    the whole-file path."""

    REG = {
        "J1": {"name": "one", "entries": [{"name": "handler_a",
                                           "file": "app/svc.py"}]},
        "J2": {"name": "two", "entries": [{"name": "leaf", "file": "app/leaf.py"}]},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = _db_on_disk(self.tmp, build_fixture())
        self.registry = _registry_file(self.tmp, self.REG)
        self.repo, self.run = _git_repo(self.tmp, {"app/svc.py": 22})

    def _commit_new_file(self, rel):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write("export const x = 1\n")
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", f"add {rel}")

    def _select(self):
        return sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)

    def test_file_with_no_index_nodes_degrades_instead_of_answering_none(self):
        self._commit_new_file("app/ui/New.svelte")
        res = self._select()
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["recall_degraded"], "silent NONE for an unmapped file")
        self.assertEqual({"J1", "J2"}, {j["id"] for j in res["journeys"]})
        self.assertTrue(all(j["verify_manually"] for j in res["journeys"]))
        self.assertTrue(
            any("no symbols in the index" in w and "New.svelte" in w
                for w in res["warnings"]),
            res["warnings"],
        )

    def test_one_mapped_file_does_not_mask_an_unmapped_one(self):
        # per-file node sets, not a running total: a commit touching handler_a AND
        # a file the index has never seen must still degrade.
        full = os.path.join(self.repo, "app", "svc.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[11] = "changed = 1\n"  # inside handler_a (10-20)
        with open(full, "w") as fh:
            fh.writelines(lines)
        with open(os.path.join(self.repo, "app", "ui.jsx"), "w") as fh:
            fh.write("export const y = 2\n")
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "mixed")
        res = self._select()
        self.assertTrue(res["recall_degraded"], "unmapped file masked by a mapped one")
        self.assertEqual({"J1", "J2"}, {j["id"] for j in res["journeys"]})
        # J1 was genuinely selected by the closure, so it is not a bare degrade row
        j1 = next(j for j in res["journeys"] if j["id"] == "J1")
        self.assertNotIn("reason", j1)
        self.assertGreater(res["seed_symbols"], 0)


if __name__ == "__main__":
    unittest.main()
