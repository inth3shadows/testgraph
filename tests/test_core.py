"""Unit tests for the closure + integrity guard, on a synthetic fixture db so
they run without codegraph. Covers S2 (guard blocks a corrupted index) and the
recall-critical closure behaviors (imports edge + file-expansion; leaf stays
tight)."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
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
        CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, indexed_at INT);
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
        # unrelated to handler_a, no edges, not a registered entry -- the
        # actual issue-#63 blind spot when both are edited in one diff.
        ("function:other_in_svc", "function", "other_in_svc", "other_in_svc",
         "app/svc.py", 1, 3),
        ("function:leaf", "function", "leaf", "leaf", "app/leaf.py", 1, 5),
        # a second, untouched symbol in the same file that IS reached from
        # elsewhere -- lets a test tell a whole-file seed set (which would
        # pull this in) apart from a range-precise one (which would not).
        ("function:leaf_sibling", "function", "leaf_sibling", "leaf_sibling",
         "app/leaf.py", 7, 10),
        ("function:external_caller", "function", "external_caller",
         "external_caller", "app/othercaller.py", 1, 5),
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
        # leaf_sibling IS reached from another file -- leaf itself is not.
        ("function:external_caller", "function:leaf_sibling", "calls", None, None),
    ]
    conn.executemany(
        "INSERT INTO edges(source,target,kind,metadata,provenance) VALUES (?,?,?,?,?)",
        edges,
    )
    conn.executemany(
        "INSERT INTO files(path,language,indexed_at) VALUES (?,?,?)",
        [("app/config.py", "python", int(time.time() * 1000) + 60000)],
    )
    conn.commit()
    return conn


class ClosureFilesTests(unittest.TestCase):
    """Issue #63 PR review: `closure_files` must not silently treat a
    dangling edge (an id `edges` names that `nodes` has no row for -- the
    same kind of drift `integrity.content_drift` exists elsewhere to catch)
    as "resolves to no other file". That reads identically to a legitimate
    confinement and would manufacture a false signal out of an untrustworthy
    index."""

    def setUp(self):
        self.conn = build_fixture()

    def test_all_ids_resolved_returns_the_file_set(self):
        self.assertEqual(
            dbmod.closure_files(self.conn, {"function:leaf"}), {"app/leaf.py"}
        )

    def test_a_dangling_id_returns_none_not_a_partial_set(self):
        self.assertIsNone(
            dbmod.closure_files(self.conn, {"function:leaf", "function:ghost"})
        )


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

    def test_suspended_spot_check_warns_instead_of_passing_silently(self):
        """A suspended check must not return a green. `append`'s floor passes on
        7 fabricated edges (#66), so counting it asserts an integrity guarantee
        the index cannot keep. Suspending skips the floor — and must SAY so,
        because a check that is silently skipped is indistinguishable from one
        that passed."""
        spot = {
            "get_settings": {
                "min_caller_edges": 10,  # would block if it were counted
                "file": "config.py",
                "suspended": "edges fabricated by the resolver (#66)",
            }
        }
        blocking, warnings = integrity.check(self.conn, "/nonexistent", spot)
        self.assertEqual(blocking, [], "a suspended check must not block")
        self.assertTrue(
            any("suspended" in w and "get_settings" in w for w in warnings),
            f"suspension was silent, not warned: {warnings}",
        )

    def test_blank_suspension_still_counts(self):
        """An empty or whitespace `suspended` is not a suspension — otherwise a
        stray key silently disarms the one check `codegraph sync` cannot clear."""
        spot = {
            "get_settings": {
                "min_caller_edges": 10,
                "file": "config.py",
                "suspended": "   ",
            }
        }
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
            "INSERT INTO files(path,language,indexed_at) VALUES (?,?,?)",
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


class SharedEntryTests(unittest.TestCase):
    """A symbol that is the hinge between two flows is an entry of both. Keying
    one journey per node kept only the last and dropped the rest — silently, and
    invisibly to `unresolved()`, which re-resolves per entry rather than reading
    the map. signedintake's `simulatePaymentCompletion` (J5 and J11) is the real
    case; `handler_a` stands in for it here."""

    REG = {
        "journeys": {
            "J1": {"name": "first", "entries": [{"name": "handler_a",
                                                 "file": "app/svc.py"}]},
            "J2": {"name": "second", "entries": [{"name": "handler_a",
                                                  "file": "app/svc.py"}]},
        },
        "spot_checks": {},
    }

    def setUp(self):
        self.conn = build_fixture()

    def test_both_journeys_claim_the_shared_node(self):
        entry_map = reg.resolve_entries(self.conn, self.REG)
        claims = [jids for jids in entry_map.values()]
        self.assertEqual(len(claims), 1, "one node, shared")
        self.assertEqual(claims[0], {"J1", "J2"})

    def test_map_row_lists_both_journeys(self):
        rows = exp.build_map(self.conn, self.REG)
        row = next(r for rs in rows.values() for r in rs
                   if r["symbol"] == "handler_a")
        self.assertEqual(row["journeys"], ["J1", "J2"])

    def test_selection_returns_both_journeys(self):
        seeds = set(dbmod.resolve_symbol(self.conn, "handler_a", "app/svc.py"))
        impacted = dbmod.impacted_closure(self.conn, seeds)
        entry_map = reg.resolve_entries(self.conn, self.REG)
        touched = {}
        for nid in impacted.keys() & set(entry_map):
            for jid in entry_map[nid]:
                touched.setdefault(jid, set()).add(nid)
        self.assertEqual(set(touched), {"J1", "J2"})


class JourneySortKeyTests(unittest.TestCase):
    """Journey ids are displayed in registry order to a human reader. Plain
    string sort only misorders once a registry passes nine journeys, so this is
    a defect no small fixture would surface."""

    def test_double_digit_ids_sort_after_single_digit(self):
        ids = ["J10", "J2", "J1", "J13", "J9"]
        self.assertEqual(
            sorted(ids, key=reg.journey_sort_key),
            ["J1", "J2", "J9", "J10", "J13"],
        )

    def test_ids_without_a_number_keep_a_stable_place(self):
        ids = ["J2", "checkout", "J10", "J1"]
        self.assertEqual(
            sorted(ids, key=reg.journey_sort_key),
            ["J1", "J2", "J10", "checkout"],
        )

    def test_prefix_groups_do_not_interleave(self):
        ids = ["B2", "A10", "A2", "B1"]
        self.assertEqual(
            sorted(ids, key=reg.journey_sort_key),
            ["A2", "A10", "B1", "B2"],
        )

    def test_rendered_map_lists_journeys_in_numeric_order(self):
        registry = {
            "journeys": {
                jid: {"name": jid.lower(), "entries": []}
                for jid in ("J1", "J2", "J10", "J11")
            },
            "spot_checks": {},
        }
        md = exp.render_markdown({}, registry, {"repo": "r", "schema": 8,
                                                "commit": "abc1234",
                                                "symbols": 0})
        listed = [ln.split()[1].strip("*") for ln in md.splitlines()
                  if ln.startswith("- **J")]
        self.assertEqual(listed, ["J1", "J2", "J10", "J11"])


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
        # A real repo, because provenance is now required: this fixture used to be
        # a bare temp directory and the export happily wrote a map stamped
        # `unknown` (issue #25). The target of a real run is always a git repo.
        for cmd in (("init", "-q"), ("config", "user.email", "t@t"),
                    ("config", "user.name", "t")):
            subprocess.run(("git",) + cmd, cwd=self.tmp, check=True,
                           capture_output=True)
        with open(os.path.join(self.tmp, "seed.txt"), "w") as fh:
            fh.write("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.tmp, check=True,
                       capture_output=True)
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

    def test_unpinned_schema_warning_reaches_the_written_file(self):
        # the reviewer's reproduction for #23, end to end: dropping the schema pin
        # printed WARN to stderr and wrote a map containing zero occurrences of it.
        with open(self.reg) as fh:
            registry = json.load(fh)
        del registry["codegraph_schema_version"]
        with open(self.reg, "w") as fh:
            json.dump(registry, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = exp.main(["--repo", self.tmp, "--registry", self.reg,
                           "--into-target"])
        self.assertEqual(rc, 0)
        self.assertIn("unpinned", err.getvalue())
        with open(os.path.join(self.tmp, ".testgraph", "journey-map.md")) as fh:
            written = fh.read()
        self.assertIn("unpinned", written)

    def test_warnings_reach_the_json_sidecar_too(self):
        with open(self.reg) as fh:
            registry = json.load(fh)
        del registry["codegraph_schema_version"]
        with open(self.reg, "w") as fh:
            json.dump(registry, fh)
        out_json = os.path.join(self.tmp, "map.json")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = exp.main(["--repo", self.tmp, "--registry", self.reg,
                           "--out", os.path.join(self.tmp, "m.md"),
                           "--json", out_json])
        self.assertEqual(rc, 0)
        with open(out_json) as fh:
            payload = json.load(fh)
        self.assertTrue(
            any("unpinned" in w for w in payload["meta"]["warnings"]),
            payload["meta"],
        )

    def test_drift_warnings_reach_stderr_and_survive_a_blocked_run(self):
        # appended AFTER the print loop, drift warnings reached neither the terminal
        # nor — on a run that then blocked — the file, discarding the one signal that
        # the registry is stale on exactly the runs most likely to carry it.
        with open(self.reg) as fh:
            registry = json.load(fh)
        # handler_a is in the fixture index; nothing in this repo defines it
        registry["journeys"]["J1"]["entries"] = [
            {"name": "handler_a", "file": "app/svc.py"}
        ]
        with open(os.path.join(self.tmp, "app_svc_placeholder.py"), "w") as fh:
            fh.write("x = 1\n")
        os.makedirs(os.path.join(self.tmp, "app"), exist_ok=True)
        with open(os.path.join(self.tmp, "app", "svc.py"), "w") as fh:
            fh.write("y = 2\n")  # exists, does not define handler_a
        with open(self.reg, "w") as fh:
            json.dump(registry, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            exp.main(["--repo", self.tmp, "--registry", self.reg, "--into-target"])
        self.assertIn("no definition", err.getvalue())

        # and again on a run that blocks: the warning must still be printed
        registry["codegraph_schema_version"] = 99
        with open(self.reg, "w") as fh:
            json.dump(registry, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = exp.main(["--repo", self.tmp, "--registry", self.reg,
                           "--into-target"])
        self.assertEqual(rc, 2)
        self.assertIn("no definition", err.getvalue())

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


class MarkdownRenderingTests(unittest.TestCase):
    """`render_markdown` produces the only artifact an agent actually reads, and
    it had no test at all — the unit tests asserted `build_map`'s dicts and the
    integration tests asserted the file merely existed.

    Issue #24: rows are keyed by SYMBOL, because line numbers are frozen at the
    generation commit and the agent's own edit has already shifted them. The table
    used to lead with `lines`, inviting exactly the lookup the skill's rules
    forbid."""

    REG = {
        "journeys": {
            "J1": {"name": "one", "entries": [{"name": "handler_a",
                                               "file": "app/svc.py"}]},
            "JW": {"name": "weak", "entries": [{"name": "mid_a",
                                                "file": "app/conf.py"}]},
        },
        "spot_checks": {},
    }
    META = {"repo": "/r", "schema": 8, "commit": "abc1234", "symbols": 4}

    def setUp(self):
        self.conn = build_fixture()
        self.rows = exp.build_map(self.conn, self.REG)
        self.md = exp.render_markdown(self.rows, self.REG, self.META)

    def _header(self):
        # `next()` with no default raised StopIteration on the very regression
        # this class exists to catch, so the column-order failure surfaced as an
        # ERROR with no message instead of the assertion written for it.
        header = next(
            (l for l in self.md.splitlines() if l.startswith("|") and "|---" not in l),
            None,
        )
        self.assertIsNotNone(header, "no table header rendered at all")
        return header

    def _header_cells(self):
        return [c.strip() for c in self._header().strip("|").split("|")]

    def test_symbol_is_the_first_column(self):
        cells = self._header_cells()
        self.assertEqual(cells[0], "symbol", f"lookup key is not first: {cells}")
        # matched on substance, not on the caption's exact spelling — pinning the
        # full string meant rewording the caveat killed the test with a ValueError
        # (the mistake test_skill_contract already learned; see its `squeezed`).
        lines_col = [i for i, c in enumerate(cells) if c.startswith("lines")]
        self.assertTrue(lines_col, f"no lines column: {cells}")
        self.assertGreater(
            lines_col[0], 0,
            "the stale hint must not be the column an agent reads first",
        )

    def test_every_row_renders_its_symbol_and_journeys(self):
        for path, rows in self.rows.items():
            section = self.md.split(f"### `{path}`")[1].split("###")[0]
            for r in rows:
                line = next(
                    (l for l in section.splitlines()
                     if l.startswith(f"| `{r['symbol']}`")), None,
                )
                self.assertIsNotNone(line, f"{path}:{r['symbol']} not rendered")
                cells = [c.strip() for c in line.strip("|").split("|")]
                self.assertEqual(
                    set(cells[1].replace("!", "").split()), set(r["journeys"]),
                    f"{path}:{r['symbol']} renders {cells[1]!r}",
                )
                self.assertEqual(
                    cells[2], f"{r['lines'][0]}–{r['lines'][1]}",
                    f"{path}:{r['symbol']} line hint misrendered",
                )

    def test_weak_journey_carries_the_bang(self):
        # `!` is the map's only safety marker; rendering it on the wrong journey,
        # or dropping it, is silent under-warning.
        section = self.md.split("### `app/conf.py`")[1]
        base = next(l for l in section.splitlines() if l.startswith("| `base`"))
        self.assertIn("JW!", base)
        own = next(l for l in section.splitlines() if l.startswith("| `mid_a`"))
        self.assertIn("JW", own)
        self.assertNotIn("JW!", own, "entry reached at full confidence was flagged")

    def test_staleness_of_line_numbers_is_stated_not_implied(self):
        # an agent that matches by line after an insertion reads the wrong
        # symbol's journeys, which is the #24 failure. The artifact must say so
        # itself — it outlives the run and carries no other warning.
        self.assertIn("by name", self.md)
        self.assertRegex(self.md, r"frozen at the commit above")
        self.assertIn("hint", self._header())

    def test_unchecked_entries_render_as_a_footnote_not_the_banner(self):
        # this rode in `meta` and was never rendered at all — dead data behind a
        # claim that the map carried it. It must appear, and it must NOT trip the
        # "not fully trustworthy" integrity banner, which prescribes a re-index that
        # cannot clear an unverifiable entry.
        md = exp.render_markdown(
            self.rows, self.REG,
            {**self.META, "unchecked_entries": [("J9", "mount", "web/App.svelte")]},
        )
        self.assertIn("not verified against source", md)
        self.assertIn("web/App.svelte", md)
        self.assertNotIn("not fully trustworthy", md)
        # and nothing is claimed when there is nothing to claim
        self.assertNotIn("not verified against source", self.md)

    def test_generation_commit_is_stamped(self):
        # the skill's staleness escalation keys off this stamp.
        self.assertIn("generated from commit `abc1234`", self.md)

    def test_dirty_stamp_is_explained_in_the_artifact(self):
        # issue #25: a `-dirty` stamp that the reader cannot interpret is no better
        # than no stamp. The explanation must ship inside the file, which outlives
        # the run and carries no other warning.
        md = exp.render_markdown(self.rows, self.REG, {**self.META,
                                                       "commit": "abc1234-dirty"})
        self.assertIn("generated from commit `abc1234-dirty`", md)
        self.assertIn("uncommitted changes", md)
        self.assertIn("regenerate after committing", md.lower())
        # and a clean stamp must NOT carry the warning, or it means nothing
        self.assertNotIn("uncommitted changes", self.md)

    def test_integrity_warnings_are_rendered_into_the_artifact(self):
        """Issue #23: only *blocking* problems stopped the write; warnings went to
        stderr and never reached the file. "N source files newer than the index" is
        the exact "this map under-reports" signal, and it was visible only in the
        terminal of the run that produced it — contradicting export.py's own stated
        reason for blocking on a corrupt index, that the file outlives the run."""
        warned = exp.render_markdown(
            self.rows, self.REG,
            {**self.META, "warnings": ["codegraph schema version unpinned",
                                       "3 source file(s) newer than the index"]},
        )
        self.assertIn("codegraph schema version unpinned", warned)
        self.assertIn("3 source file(s) newer than the index", warned)
        self.assertIn("under-report", warned)
        # rendered as a blockquote near the top, not buried under the tables
        head, _, tail = warned.partition("## Symbols by file")
        self.assertIn("schema version unpinned", head)
        self.assertNotIn("schema version unpinned", tail)
        # a clean run must not carry an empty warning block, or it means nothing
        self.assertNotIn("not fully trustworthy", self.md)

    def test_artifact_and_skill_agree_on_the_unattributable_edit(self):
        """The artifact and SKILL.md are two documents stating one rule, and
        nothing pinned their agreement — so they drifted apart inside the very
        commit that fixed #24. The map's preamble narrowed line ranges to
        "disambiguate two symbols sharing one", while the skill still called them a
        general hint. That gap is a live under-report: the honeyslate map has rows
        named `sqlalchemy.orm` and `_settings`, so an agent editing an import block
        or a module-level binding recognises no symbol it touched, and the absence
        rule then licenses "no journeys affected" for a J6 (sign-in) file."""
        with open(
            os.path.join(ROOT_DIR, "skills", "testgraph-verify", "SKILL.md"),
            encoding="utf-8",
        ) as fh:
            skill = re.sub(r"\s+", " ", fh.read())
        for doc, name in ((self.md, "the map"), (skill, "SKILL.md")):
            squeezed = re.sub(r"\s+", " ", doc)
            self.assertRegex(
                squeezed, r"import nodes and module-level bindings|"
                r"[Ii]mport nodes \(`sqlalchemy\.orm`\) and module-level bindings",
                f"{name} does not warn that some rows are not named for functions",
            )
            self.assertIn(
                "unknown", squeezed,
                f"{name} does not name the unattributable edit as unknown",
            )
        self.assertRegex(
            skill, r"use the line range as the fallback key",
            "SKILL.md dropped the line-range fallback the map now points agents to",
        )
        self.assertRegex(
            re.sub(r"\s+", " ", self.md), r"fall back to the range when you cannot",
            "the map dropped the line-range fallback SKILL.md relies on",
        )


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


class CommitStampTests(unittest.TestCase):
    """Issue #25: the stamp used to fall back to the literal string `"unknown"` on
    any git failure, which disabled the consumer's staleness escalation while
    looking stamped — provenance failing open. It also reported HEAD for a dirty
    tree, so a map built from uncommitted code claimed clean provenance."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.run = _git_repo(self.tmp, {"app/svc.py": 4})

    def _write(self, rel, text="x = 1\n"):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def test_clean_tree_stamps_the_short_sha(self):
        stamp = exp.commit_stamp(self.repo)
        self.assertRegex(stamp, r"^[0-9a-f]{7,}$")

    def test_modified_tracked_file_is_dirty(self):
        self._write("app/svc.py", "changed = 1\n")
        self.assertTrue(exp.commit_stamp(self.repo).endswith("-dirty"))

    def test_staged_change_is_dirty(self):
        self._write("app/new.py")
        self.run("git", "add", "-A")
        self.assertTrue(exp.commit_stamp(self.repo).endswith("-dirty"))

    def test_untracked_source_is_dirty(self):
        # codegraph indexes it, so the map can describe code in no commit.
        self._write("app/brand_new.py")
        self.assertTrue(exp.commit_stamp(self.repo).endswith("-dirty"))

    def test_changes_the_indexer_ignores_do_not_mark_the_tree_dirty(self):
        # `--into-target` writes .testgraph/journey-map.md, codegraph keeps
        # .codegraph/, and a repo usually has a stray doc or two (honeyslate has
        # two untracked ones right now). A marker that is always on is one the
        # reader learns to skip, so only indexed extensions count.
        self._write(".testgraph/journey-map.md", "# map\n")
        self._write(".codegraph/codegraph.db", "binary-ish\n")
        self._write("NOTES.md", "# notes\n")
        self.assertFalse(
            exp.commit_stamp(self.repo).endswith("-dirty"),
            "a change the indexer cannot see marked the target dirty",
        )

    def test_uncommitted_frontend_file_is_dirty_too(self):
        # PRODUCT_EXT is shared with the selector, so widening it (#21) widened
        # this automatically rather than leaving a Python-only provenance check.
        self._write("web/App.svelte", "<script>let x = 1</script>\n")
        self.assertTrue(exp.commit_stamp(self.repo).endswith("-dirty"))

    def test_renamed_source_is_dirty(self):
        # a rename's status line carries `old -> new`; reading only one side
        # would miss a moved module, the case select.py already handles with -M.
        self.run("git", "mv", "app/svc.py", "app/renamed.py")
        self.assertTrue(exp.commit_stamp(self.repo).endswith("-dirty"))

    def test_plain_directory_inside_a_repo_does_not_borrow_its_HEAD(self):
        # `git -C` walks up, so a non-repo directory nested inside a repo used to
        # stamp successfully with the ENCLOSING project's commit — worse than
        # `unknown`, because the consumer's "far behind HEAD" comparison runs
        # against a history that keeps moving and the map reads fresh forever.
        nested = os.path.join(self.repo, "vendor", "unrelated")
        os.makedirs(nested)
        with open(os.path.join(nested, "thing.py"), "w") as fh:
            fh.write("x = 1\n")
        with self.assertRaises(exp.StampError) as caught:
            exp.commit_stamp(nested)
        self.assertIn("no git-tracked files", str(caught.exception))

    def test_unborn_head_blocks_with_its_own_reason(self):
        # a repo with no commits has no provenance to record; blocking is the
        # deliberate choice, but the message must say which case it is.
        fresh = os.path.join(self.tmp, "fresh")
        os.makedirs(fresh)
        subprocess.run(["git", "init", "-q"], cwd=fresh, check=True,
                       capture_output=True)
        with open(os.path.join(fresh, "app.py"), "w") as fh:
            fh.write("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=fresh, check=True,
                       capture_output=True)
        with self.assertRaises(exp.StampError) as caught:
            exp.commit_stamp(fresh)
        self.assertIn("no commits yet", str(caught.exception))

    def test_missing_git_binary_still_fails_closed(self):
        # git absent from PATH raises FileNotFoundError instead of returning
        # non-zero, which escaped the fail-closed path entirely and killed the
        # export with a traceback and exit 1.
        with unittest.mock.patch.object(
            exp.subprocess, "run", side_effect=FileNotFoundError("no git")
        ):
            with self.assertRaises(exp.StampError) as caught:
                exp.commit_stamp(self.repo)
        self.assertIn("cannot run git", str(caught.exception))

    def test_subdirectory_target_ignores_changes_outside_itself(self):
        # `git status` reports the whole repo even when run from a subdirectory, so
        # a `--repo <repo>/backend` target used to be marked dirty by an unrelated
        # edit elsewhere in the monorepo. The map only describes its own target.
        self._write("other/unrelated.py", "changed = 1\n")
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "add other")
        self._write("other/unrelated.py", "changed = 2\n")
        self.assertFalse(
            exp.commit_stamp(os.path.join(self.repo, "app")).endswith("-dirty"),
            "a change outside the target subtree marked the target dirty",
        )
        # ...and a change INSIDE it still does
        self._write("app/svc.py", "changed = 3\n")
        self.assertTrue(
            exp.commit_stamp(os.path.join(self.repo, "app")).endswith("-dirty")
        )

    def test_target_nested_under_a_test_named_directory_still_goes_dirty(self):
        # `git status` prints repo-root-relative paths while `ls-files` prints
        # cwd-relative ones. For `--repo <repo>/e2e/app` every status path gained a
        # leading `e2e/`, `_is_test` matched that segment, and the map was stamped
        # clean whatever was uncommitted — the bug commit_stamp exists to prevent,
        # reintroduced by the path base mismatch.
        self._write("e2e/app/svc.py", "x = 1\n")
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "add nested target")
        self._write("e2e/app/svc.py", "x = 2\n")
        self.assertTrue(
            exp.commit_stamp(os.path.join(self.repo, "e2e", "app")).endswith("-dirty"),
            "uncommitted change under a test-named parent stamped clean",
        )

    def test_uncommitted_test_file_is_not_dirty(self):
        # `impacted_closure` walks callers, so a test symbol reaches no journey
        # entry and no test file has ever produced a row. An edit that cannot
        # change a row must not stamp the persisted artifact as untrustworthy.
        self._write("tests/test_svc.py", "def test_x():\n    assert True\n")
        self._write("types/api.d.ts", "export declare const x: number;\n")
        self.assertFalse(
            exp.commit_stamp(self.repo).endswith("-dirty"),
            "a change that cannot appear in the map marked it dirty",
        )

    def test_provenance_failure_does_not_blame_the_index(self):
        # the remedy for a corrupt index is a multi-minute `codegraph index`
        # rebuild; sending someone there because --repo is not a git repo wastes
        # their time on the wrong fix.
        plain = os.path.join(self.tmp, "plain2")
        os.makedirs(os.path.join(plain, ".codegraph"))
        dst = sqlite3.connect(os.path.join(plain, ".codegraph", "codegraph.db"))
        build_fixture().backup(dst)
        dst.close()
        registry = _registry_file(
            self.tmp,
            {"J1": {"name": "one", "entries": [{"name": "handler_a",
                                                "file": "app/svc.py"}]}},
            name="reg-plain2.json",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = exp.main(["--repo", plain, "--registry", registry,
                           "--into-target"])
        self.assertEqual(rc, 2)
        self.assertIn("provenance unverifiable", err.getvalue())
        self.assertNotIn("index not trustworthy", err.getvalue())

    def test_non_git_target_raises_instead_of_stamping_unknown(self):
        with self.assertRaises(exp.StampError) as caught:
            exp.commit_stamp(os.path.join(self.tmp, "not-a-repo"))
        self.assertIn("not a git working tree", str(caught.exception))

    def test_export_blocks_and_writes_nothing_without_provenance(self):
        # the whole point: no stamp -> no map, on the same path as a corrupt index.
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(os.path.join(plain, ".codegraph"))
        db = os.path.join(plain, ".codegraph", "codegraph.db")
        dst = sqlite3.connect(db)
        build_fixture().backup(dst)
        dst.close()
        registry = _registry_file(
            self.tmp,
            {"J1": {"name": "one", "entries": [{"name": "handler_a",
                                                "file": "app/svc.py"}]}},
            name="reg-plain.json",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = exp.main(["--repo", plain, "--registry", registry,
                           "--into-target"])
        self.assertEqual(rc, 2)
        self.assertIn("not a git working tree", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(plain, ".testgraph")))


class LiveEntryDriftTests(unittest.TestCase):
    """Issue #7: the registry and the index can AGREE while both are stale against
    the source. Rename a handler, run before re-indexing, and the stale node still
    resolves — every answer is then about a symbol that no longer exists.
    `registry.live_drift` is the only check in the pipeline that reads the working
    tree, so it is the only one that can catch this.

    Implemented as a Python `ast` parse rather than the RunEcho MCP call the issue
    proposed: testgraph is a CLI with no MCP client, and stdlib `ast` answers the
    same question for the only language any journey has entries in. Non-Python
    entries are reported `unchecked` instead of silently passing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "backend", "app", "routers"))
        self._write("backend/app/routers/tasks.py", """
import functools
from .other import reexported

@functools.wraps(None)
def create_task():
    pass

class Scheduler:
    def sweep(self):
        pass

_settings = object()
""")

    def _write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def _reg(self, name, rel="routers/tasks.py"):
        return {"journeys": {"J1": {"name": "one",
                                    "entries": [{"name": name, "file": rel}]}}}

    def test_registry_file_is_a_suffix_not_a_repo_relative_path(self):
        # `resolve_symbol` matches the registry's `file` with a LIKE, so
        # `routers/tasks.py` means `backend/app/routers/tasks.py`. Joining it onto
        # the repo root instead reported all 16 of honeyslate's real entries as
        # "file is gone" — the bug this test exists to keep out.
        self.assertEqual(reg.live_drift(self.tmp, self._reg("create_task")), [])

    def test_decorated_function_is_found(self):
        self.assertEqual(reg.live_drift(self.tmp, self._reg("create_task")), [])

    def test_method_inside_a_class_is_found(self):
        # honeyslate anchors J8 on `sweep`; a top-level-only check would call
        # every method drift.
        self.assertEqual(reg.live_drift(self.tmp, self._reg("sweep")), [])

    def test_module_level_binding_is_found(self):
        self.assertEqual(reg.live_drift(self.tmp, self._reg("_settings")), [])

    def test_reexported_symbol_is_not_drift(self):
        # an import IS how the name becomes available under that path; calling it
        # drift would block real runs to report a freshness problem.
        self.assertEqual(reg.live_drift(self.tmp, self._reg("reexported")), [])

    def test_renamed_symbol_is_reported(self):
        drift = reg.live_drift(self.tmp, self._reg("create_task_OLD"))
        self.assertEqual(len(drift), 1)
        self.assertIn("no definition", drift[0][3])

    def test_missing_file_is_reported(self):
        drift = reg.live_drift(self.tmp, self._reg("create_task", "routers/gone.py"))
        self.assertEqual(len(drift), 1)
        self.assertIn("no file matching", drift[0][3])

    def test_unparseable_file_is_reported_not_swallowed(self):
        self._write("backend/app/routers/broken.py", "def (:\n")
        drift = reg.live_drift(self.tmp, self._reg("x", "routers/broken.py"))
        self.assertEqual(len(drift), 1)
        self.assertIn("cannot parse", drift[0][3])

    def test_non_python_entry_is_unchecked_on_its_own_channel(self):
        # NOT drift: emitting it as one put "the index was not fully trustworthy"
        # in every exported map the moment a frontend journey existed, prescribing
        # a `codegraph index` that can never clear an unverifiable entry.
        registry = self._reg("mount", "web/App.svelte")
        self.assertEqual(reg.live_drift(self.tmp, registry), [])
        self.assertEqual(
            reg.unchecked_entries(registry), [("J1", "mount", "web/App.svelte")]
        )

    def test_remedy_depends_on_the_reason(self):
        # one hard-coded "run `codegraph index`" was wrong for most reasons:
        # live_drift reads the working tree, the rest of select reads committed
        # history, and a registry typo is not an index problem at all.
        self.assertIn("commit first",
                      reg.remedy_for("no definition of that name in the file"))
        self.assertIn("registry entry",
                      reg.remedy_for("no file matching that path in the tree"))
        self.assertIn("syntax error", reg.remedy_for("cannot parse (SyntaxError)"))
        self.assertNotIn("codegraph index",
                         reg.remedy_for("cannot parse (SyntaxError)"))

    def test_non_utf8_source_is_reported_not_raised(self):
        # a PEP-263 latin-1 file raised UnicodeDecodeError — a ValueError, caught by
        # neither clause — turning "reported, never blocking" into a traceback in
        # both select and export.
        path = os.path.join(self.tmp, "backend", "app", "routers", "legacy.py")
        with open(path, "wb") as fh:
            fh.write(b"# -*- coding: latin-1 -*-\ndef caf\xe9():\n    pass\n")
        drift = reg.live_drift(self.tmp, self._reg("cafe", "routers/legacy.py"))
        self.assertEqual(len(drift), 1)
        self.assertIn("no definition", drift[0][3])

    def test_null_byte_source_is_reported_not_raised(self):
        # ast.parse raises ValueError (not SyntaxError) for a NUL byte, so the
        # widened except clause is load-bearing beyond the decode case.
        path = os.path.join(self.tmp, "backend", "app", "routers", "nul.py")
        with open(path, "wb") as fh:
            fh.write(b"def ok():\n    pass\n\x00")
        drift = reg.live_drift(self.tmp, self._reg("ok", "routers/nul.py"))
        self.assertEqual(len(drift), 1)
        self.assertIn("cannot parse", drift[0][3])

    def test_function_local_binding_does_not_mask_drift(self):
        self._write("backend/app/routers/local.py",
                    "def f():\n    create_task = object()\n    return create_task\n")
        drift = reg.live_drift(self.tmp, self._reg("create_task", "routers/local.py"))
        self.assertEqual(len(drift), 1, "a function-local binding satisfied a "
                                       "module-level entry")

    def test_nested_inner_function_does_not_satisfy_a_module_entry(self):
        # nothing inside a function body is importable, so a nested
        # `def create_task()` must not clear a module-level entry.
        self._write("backend/app/routers/nested.py",
                    "def outer():\n    def create_task():\n        pass\n"
                    "    return create_task\n")
        drift = reg.live_drift(self.tmp, self._reg("create_task", "routers/nested.py"))
        self.assertEqual(len(drift), 1, "a nested inner def satisfied the entry")

    def test_suffix_match_is_anchored_on_a_path_separator(self):
        # `endswith("routers/tasks.py")` also matched `.../xrouters/tasks.py`
        self._write("backend/app/xrouters/tasks.py", "def create_task():\n    pass\n")
        drift = reg.live_drift(self.tmp, self._reg("create_task", "outers/tasks.py"))
        self.assertEqual(len(drift), 1)
        self.assertIn("no file matching", drift[0][3])

    def test_conditional_module_level_import_still_counts(self):
        self._write("backend/app/routers/cond.py",
                    "try:\n    from .x import handler\nexcept ImportError:\n"
                    "    handler = None\n")
        self.assertEqual(
            reg.live_drift(self.tmp, self._reg("handler", "routers/cond.py")), []
        )

    def test_select_surfaces_drift_as_a_warning_and_a_field(self):
        repo, run = _git_repo(self.tmp2(), {"app/svc.py": 22})
        db = _db_on_disk(self.tmp, build_fixture(), name="drift.db")
        registry = _registry_file(
            self.tmp,
            {"J1": {"name": "one", "entries": [{"name": "handler_a",
                                                "file": "app/svc.py"}]}},
            name="reg-drift.json",
        )
        # the index has handler_a; the source (22 lines of `line_N = N`) does not
        res = sel.select(repo, "HEAD", "HEAD", db, registry)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(
            [d["entry"] for d in res["entry_drift"]], ["handler_a"],
            res["entry_drift"],
        )
        self.assertTrue(
            any("no definition" in w for w in res["warnings"]), res["warnings"]
        )
        self.assertEqual(res["entries_unchecked"], [])

    def tmp2(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d


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
            any("cannot be trusted for" in w and "New.svelte" in w
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


class ClosureConfinedTests(unittest.TestCase):
    """Issue #63: seeds that resolve fine but whose closure never leaves the
    file they started in used to be indistinguishable from a confident,
    correct `NONE` — no signal at all. `function:leaf` (app/leaf.py) has zero
    outbound edges in the fixture (see ClosureTests.test_leaf_stays_tight),
    so editing it is exactly this blind spot."""

    REG = {"J1": {"name": "one", "entries": [{"name": "handler_a",
                                              "file": "app/svc.py"}]}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = _db_on_disk(self.tmp, build_fixture())
        self.registry = _registry_file(self.tmp, self.REG)
        self.repo, self.run = _git_repo(
            self.tmp, {"app/svc.py": 22, "app/leaf.py": 5, "app/config.py": 5}
        )

    def _edit_leaf(self):
        full = os.path.join(self.repo, "app", "leaf.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[1] = "changed = 1\n"  # inside function:leaf (1-5)
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "edit leaf")

    def test_confined_closure_is_flagged(self):
        self._edit_leaf()
        res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
        self.assertEqual(res["status"], "OK")
        self.assertFalse(res["recall_degraded"], "not the no-node case")
        self.assertEqual(res["closure_confined"], ["app/leaf.py"])
        # The signal is NOT on the capped `warnings` channel (see hook.py's
        # dedicated line) -- it's carried structurally on the result, and
        # `_render` turns it into its own NOTE line.
        self.assertNotIn(
            "did not leave the file", " ".join(res["warnings"]), res["warnings"]
        )
        rendered = sel._render(res)
        self.assertIn("app/leaf.py", rendered)
        self.assertIn("did not leave the file", rendered)
        # leaf has no journey entry, so the answer is still NONE -- the point
        # is that NONE now carries a NOTE saying it may mean UNKNOWN.
        self.assertEqual(res["journeys"], [])

    def test_change_reaching_outside_its_file_is_not_flagged(self):
        # get_settings' closure crosses into app/svc.py via the imports edge +
        # file expansion (ClosureTests.test_imports_and_file_expansion_reach_
        # handler) -- confirm a real cross-file reach produces no false
        # positive.
        full = os.path.join(self.repo, "app", "config.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[1] = "changed = 1\n"  # inside get_settings (1-5)
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "edit get_settings")
        res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
        self.assertEqual(res["closure_confined"], [])

    def test_an_edited_entry_point_with_no_callers_is_not_a_false_positive(self):
        # handler_a is ITSELF the registered J1 entry and has no callers on
        # record in this fixture, so its own closure never leaves app/svc.py
        # -- but that is not unknown, it's a confidently-selected journey.
        # Flagging it anyway was reproduced against this repo's own index
        # (issue #63 PR review): 7 of 11 "confined" files had already
        # selected a journey at confidence 1.0.
        full = os.path.join(self.repo, "app", "svc.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[11] = "changed = 1\n"  # inside handler_a (10-20)
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "edit handler_a")
        res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
        self.assertEqual(res["closure_confined"], [])
        j1 = next(j for j in res["journeys"] if j["id"] == "J1")
        self.assertEqual(j1["confidence"], 1.0)

    def test_an_entry_seed_does_not_mask_an_unrelated_confined_seed(self):
        # Both handler_a (the entry -- fine on its own) and other_in_svc (no
        # edges, no entry, the actual blind spot) are edited in ONE diff to
        # app/svc.py. Judging confinement per FILE instead of per SEED let
        # handler_a's entry-hit clear the whole file, silently swallowing the
        # NOTE for other_in_svc -- reproduced directly against this fixture
        # before the per-seed fix.
        full = os.path.join(self.repo, "app", "svc.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[0] = "changed = 1\n"    # inside other_in_svc (1-3)
        lines[11] = "changed = 1\n"   # inside handler_a (10-20)
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "edit both svc.py symbols")
        res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
        self.assertEqual(res["closure_confined"], ["app/svc.py"])
        j1 = next(j for j in res["journeys"] if j["id"] == "J1")
        self.assertEqual(j1["confidence"], 1.0)


class RenameWithEditConfinementTests(unittest.TestCase):
    """A rename that also carries edited hunks lands the new path in BOTH
    `ranges` (precise, from the hunk) and `whole_files` (the full file, from
    the rename). `function:leaf_sibling` (app/leaf.py:7-10) is untouched by
    the edit below but IS reached from app/othercaller.py -- so the
    whole-file seed set escapes app/leaf.py while the precise, edited-lines
    seed set (just `function:leaf`, 1-5) does not. Confinement must be
    judged on the precise set, or a real issue-#63 blind spot in the lines
    that actually changed gets masked by an unrelated symbol in the same
    file."""

    REG = {"J1": {"name": "one", "entries": [{"name": "handler_a",
                                              "file": "app/svc.py"}]}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = _db_on_disk(self.tmp, build_fixture())
        self.registry = _registry_file(self.tmp, self.REG)
        self.repo, self.run = _git_repo(self.tmp, {"app/oldname.py": 10})

    def test_confinement_uses_the_precise_range_not_the_whole_file(self):
        self.run("git", "mv", "app/oldname.py", "app/leaf.py")
        with open(os.path.join(self.repo, "app", "leaf.py")) as fh:
            lines = fh.readlines()
        lines[1] = "changed = 1\n"  # inside function:leaf (1-5), not the sibling (7-10)
        with open(os.path.join(self.repo, "app", "leaf.py"), "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", "rename + edit leaf")
        res = sel.select(self.repo, "HEAD~1", "HEAD", self.db, self.registry)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(
            res["closure_confined"], ["app/leaf.py"],
            "whole-file seeding pulled in leaf_sibling and masked the "
            "confined edit — confinement should track the edited lines",
        )


class ChangedFileContentDriftTests(unittest.TestCase):
    """A changed file whose bytes no longer match the indexed copy is the
    quietest way to be unmappable. The other two resolve to no node and are
    obvious; this one resolves to the WRONG node — seeds come from line ranges,
    so a file that gained lines since indexing hands the diff's numbers to
    whatever symbol used to occupy them. The answer stays confident and can be
    NARROWER than the truth, which is the one failure this selector exists to
    refuse.

    Found when the pre-push hook started running this in anger: neither
    installed target refreshes its index on commit, so every push after the
    first was going to be answered off stale spans while `integrity.check`
    downgraded it to one warning out of a capped three.

    Decided on `files.content_hash`, NOT on mtime. mtime was the first
    implementation and it is wrong in both directions: `git checkout` rewrites
    mtimes without changing a byte (false drift on every branch switch), and
    `codegraph sync` leaves indexed_at alone when content is unchanged — it
    reports "Already up to date" and the mtime warning never clears. The hash is
    what the indexer itself compares."""

    REG = {
        "J1": {"name": "one", "entries": [{"name": "handler_a",
                                           "file": "app/svc.py"}]},
        "J2": {"name": "two", "entries": [{"name": "leaf", "file": "app/leaf.py"}]},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = build_fixture()
        self.registry = _registry_file(self.tmp, self.REG)
        self.repo, self.run = _git_repo(self.tmp, {"app/svc.py": 22})

    def _index_row(self, path, content_hash):
        self.conn.execute(
            "INSERT INTO files(path,content_hash,language,indexed_at) "
            "VALUES (?,?,?,?)",
            (path, content_hash, "python", int(time.time() * 1000)),
        )
        self.conn.commit()

    def _hash_of(self, rel):
        with open(os.path.join(self.repo, rel), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def _edit_and_commit(self, rel, line_ix):
        full = os.path.join(self.repo, rel)
        with open(full) as fh:
            lines = fh.readlines()
        lines[line_ix] = "changed = 1\n"
        with open(full, "w") as fh:
            fh.writelines(lines)
        self.run("git", "add", "-A")
        self.run("git", "commit", "-qm", f"edit {rel}")

    def _select(self):
        db = _db_on_disk(self.tmp, self.conn)
        return sel.select(self.repo, "HEAD~1", "HEAD", db, self.registry)

    def test_changed_file_matching_its_indexed_hash_answers_normally(self):
        # The control, and the case that must NOT regress: the index was rebuilt
        # after the edit (what the hook's `codegraph sync` does), so the spans are
        # trustworthy and the ordinary narrow answer has to survive.
        self._edit_and_commit("app/svc.py", 11)  # inside handler_a (10-20)
        self._index_row("app/svc.py", self._hash_of("app/svc.py"))
        res = self._select()
        self.assertEqual(res["status"], "OK")
        self.assertFalse(res["recall_degraded"])
        self.assertEqual({"J1"}, {j["id"] for j in res["journeys"]})

    def test_changed_file_whose_bytes_drifted_degrades(self):
        self._index_row("app/svc.py", "0" * 64)
        self._edit_and_commit("app/svc.py", 11)
        res = self._select()
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["recall_degraded"], "stale span trusted silently")
        self.assertEqual({"J1", "J2"}, {j["id"] for j in res["journeys"]})
        self.assertTrue(
            any("app/svc.py" in w and "line spans are stale" in w
                for w in res["warnings"]),
            res["warnings"],
        )

    def test_the_seeds_it_did_find_are_kept_not_discarded(self):
        # Recall-first means ADDING doubt, not removing rows. J1 was reached by
        # the closure and stays a real selection; only J2 rides in as a bare
        # degrade row. Dropping the seeds instead would turn a stale index into a
        # LOSS of information at exactly the moment we have least to spare.
        self._index_row("app/svc.py", "0" * 64)
        self._edit_and_commit("app/svc.py", 11)
        res = self._select()
        j1 = next(j for j in res["journeys"] if j["id"] == "J1")
        j2 = next(j for j in res["journeys"] if j["id"] == "J2")
        self.assertNotIn("reason", j1)
        self.assertEqual(j1["entries_hit"], 1)  # reached by the closure, not listed
        # Only the rows that rode in on the degrade are flagged; J1 keeps the
        # confidence of the path that actually reached it. The global
        # `recall_degraded` banner carries the caveat for the answer as a whole —
        # flagging every row would erase the distinction between "reached by an
        # edge" and "listed because we cannot rule it out".
        self.assertFalse(j1["verify_manually"])
        self.assertEqual(j2.get("reason"), "change with no resolvable symbols")
        self.assertTrue(j2["verify_manually"])

    def test_a_drifted_file_nobody_touched_does_not_degrade(self):
        # Only drift INSIDE the diff is a soundness problem; elsewhere it costs
        # precision and rides the warning channel. Degrading on every unrelated
        # drifted file would make the loud signal mean nothing.
        self._index_row("app/config.py", "0" * 64)
        self._edit_and_commit("app/svc.py", 11)
        self._index_row("app/svc.py", self._hash_of("app/svc.py"))
        res = self._select()
        self.assertFalse(res["recall_degraded"])
        self.assertEqual({"J1"}, {j["id"] for j in res["journeys"]})

    def test_mtime_alone_is_not_drift(self):
        # The bug in the first implementation, pinned: `git checkout` rewrites
        # mtimes on files it did not change, and `codegraph sync` will not clear
        # that because the content is identical. Bumping mtime while the hash
        # still matches must leave the narrow answer intact.
        self._edit_and_commit("app/svc.py", 11)
        self._index_row("app/svc.py", self._hash_of("app/svc.py"))
        os.utime(os.path.join(self.repo, "app", "svc.py"), (time.time() + 3600,) * 2)
        res = self._select()
        self.assertFalse(res["recall_degraded"], "mtime bump read as content drift")
        self.assertEqual({"J1"}, {j["id"] for j in res["journeys"]})


if __name__ == "__main__":
    unittest.main()
