"""Trace-derived journey ground truth (issue #12).

Hermetic: no honeyslate, no venv, no pytest. The collection logic in
`harness/tgtrace.py` is driven directly, and the join in
`harness/ground_truth.py` runs against the same synthetic in-memory index the
rest of the suite uses. testgraph is stdlib-only and its CI has no pytest, so a
test that shelled out to a real suite would be a test that never runs.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import ground_truth as gt  # noqa: E402
from harness.plugin import tgtrace  # noqa: E402


def build_index():
    """entry -> mid -> shared, plus `stray`, which nothing reaches.

    `stray` is the fixture's whole point: it is reachable at RUNTIME (a trace
    can see it execute) and absent from the static footprint, which is exactly
    the silent-miss shape this harness exists to find."""
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
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("f:entry", "function", "entry", "entry", "app/routes.py", 1, 9),
            ("f:mid", "function", "mid", "mid", "app/svc.py", 1, 9),
            ("f:shared", "function", "shared", "shared", "app/util.py", 1, 9),
            ("f:stray", "function", "stray", "stray", "app/dyn.py", 1, 9),
            ("f:Thing.run", "method", "run", "Thing.run", "app/svc.py", 20, 29),
        ],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,metadata,provenance) VALUES (?,?,?,?,?)",
        [
            ("f:entry", "f:mid", "calls", None, None),
            ("f:mid", "f:shared", "calls", None, None),
        ],
    )
    conn.commit()
    return conn


REGISTRY = {
    "target": "fixture",
    "journeys": {
        "J1": {"name": "the one", "entries": [{"name": "entry", "file": "app/routes.py"}]},
        "J2": {"name": "untested", "entries": [{"name": "entry", "file": "app/routes.py"}]},
    },
}


class TestMappingTest(unittest.TestCase):
    def setUp(self):
        self.trace = {
            "root": "/x",
            "tests": {
                "tests/test_a.py::test_one": [["app/svc.py", "mid"]],
                "tests/test_b.py::test_two": [["app/dyn.py", "stray"]],
                "tests/test_c.py::test_three": [["app/util.py", "shared"]],
            },
        }

    def test_a_file_level_label_covers_every_test_in_it(self):
        by_j, unmapped = gt.journey_traces(self.trace, {"tests/test_a.py": ["J1"]})
        self.assertEqual(by_j["J1"], {("app/svc.py", "mid")})
        self.assertEqual(len(unmapped), 2)

    def test_a_nodeid_label_wins_over_the_file_label(self):
        by_j, _ = gt.journey_traces(
            self.trace,
            {"tests/test_a.py": ["J1"], "tests/test_a.py::test_one": ["J2"]},
        )
        self.assertEqual(by_j["J2"], {("app/svc.py", "mid")})
        self.assertNotIn("J1", by_j)

    def test_a_file_mapped_to_several_journeys_feeds_all_of_them(self):
        # Over-approximation on purpose: it inflates traced_only, which makes
        # the selector look worse, never better.
        by_j, _ = gt.journey_traces(self.trace, {"tests/test_a.py": ["J1", "J2"]})
        self.assertEqual(by_j["J1"], by_j["J2"])

    def test_an_unmapped_test_is_reported_not_silently_dropped(self):
        _, unmapped = gt.journey_traces(self.trace, {})
        self.assertEqual(len(unmapped), 3)

    def test_a_map_still_matches_when_pytest_prefixes_the_nodeid(self):
        # pytest nodeids are relative to its rootdir, which for a target with no
        # ini file is the invocation dir — so `--repo <r>/backend --tests tests`
        # and `--repo <r> --tests backend/tests` yield DIFFERENT nodeids for the
        # same test. A map keyed on one form matched nothing under the other and
        # every journey reported no_trace, which read as a clean run.
        prefixed = {
            "root": "/x",
            "tests": {"backend/tests/test_a.py::test_one": [["app/svc.py", "mid"]]},
        }
        by_j, unmapped = gt.journey_traces(prefixed, {"tests/test_a.py": ["J1"]})
        self.assertEqual(by_j["J1"], {("app/svc.py", "mid")})
        self.assertEqual(unmapped, [])

    def test_the_longest_matching_suffix_wins(self):
        trace = {"tests": {"a/b/test_x.py::t": [["app/svc.py", "mid"]]}}
        by_j, _ = gt.journey_traces(
            trace, {"test_x.py": ["J2"], "b/test_x.py": ["J1"]}
        )
        self.assertIn("J1", by_j)
        self.assertNotIn("J2", by_j)


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.conn = build_index()
        

    def test_a_traced_symbol_resolves_to_its_node(self):
        ids, missing = gt.resolve_traced(self.conn, [("app/svc.py", "mid")])
        self.assertEqual(ids, {"f:mid"})
        self.assertEqual(missing, [])

    def test_a_method_resolves_on_its_last_dotted_component(self):
        ids, missing = gt.resolve_traced(self.conn, [("app/svc.py", "Thing.run")])
        self.assertEqual(ids, {"f:Thing.run"})
        self.assertEqual(missing, [])

    def test_a_closure_frame_is_dropped_rather_than_counted_as_a_miss(self):
        # No edge kind can ever make a <locals> function seedable, so counting
        # it would pad the number with something the fix cannot reach.
        ids, missing = gt.resolve_traced(
            self.conn, [("app/svc.py", "mid.<locals>.inner")]
        )
        self.assertEqual((ids, missing), (set(), []))

    def test_a_symbol_the_index_never_saw_is_unresolved_not_a_traversal_miss(self):
        ids, missing = gt.resolve_traced(self.conn, [("app/ghost.py", "nowhere")])
        self.assertEqual(ids, set())
        self.assertEqual(missing, [("app/ghost.py", "nowhere")])

    def test_a_same_named_symbol_in_another_file_is_not_accepted_as_a_match(self):
        # `resolve_symbol` matches on a basename LIKE, so a traced
        # `vendor/svc.py:mid` the index genuinely lacks would bind to
        # `app/svc.py:mid` — and whether it counted as a miss would then be
        # decided by an unrelated node's edges.
        ids, missing = gt.resolve_traced(self.conn, [("vendor/svc.py", "mid")])
        self.assertEqual(ids, set())
        self.assertEqual(missing, [("vendor/svc.py", "mid")])

    def test_two_indexes_in_one_process_do_not_share_a_file_cache(self):
        other = build_index()
        other.execute("UPDATE nodes SET file_path = 'elsewhere/svc.py' WHERE id = 'f:mid'")
        ids, _ = gt.resolve_traced(other, [("elsewhere/svc.py", "mid")])
        self.assertEqual(ids, {"f:mid"})
        ids, _ = gt.resolve_traced(self.conn, [("app/svc.py", "mid")])
        self.assertEqual(ids, {"f:mid"})


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.conn = build_index()
        

    def _rows(self, by_journey):
        return {r["journey"]: r for r in gt.compare(self.conn, REGISTRY, by_journey)}

    def test_a_traced_symbol_inside_the_footprint_is_not_a_miss(self):
        rows = self._rows({"J1": {("app/svc.py", "mid")}})
        self.assertEqual(rows["J1"]["traced_only"], [])

    def test_a_traced_symbol_outside_the_footprint_is_a_silent_miss_source(self):
        # `stray` runs during the journey and no edge reaches it from the entry,
        # so editing `stray` would break J1 with the selector saying nothing.
        rows = self._rows({"J1": {("app/dyn.py", "stray")}})
        self.assertEqual(rows["J1"]["traced_only"], ["f:stray"])

    def test_footprint_symbols_the_suite_never_ran_are_counted_apart(self):
        rows = self._rows({"J1": {("app/svc.py", "mid")}})
        self.assertGreater(rows["J1"]["static_only"], 0)
        self.assertEqual(rows["J1"]["traced_only"], [])

    def test_a_journey_with_no_trace_is_flagged_not_scored_as_agreement(self):
        rows = self._rows({"J1": {("app/svc.py", "mid")}})
        self.assertTrue(rows["J2"]["no_trace"])
        self.assertEqual(rows["J2"]["traced_only"], [])

    def test_the_render_shouts_a_miss_and_names_the_symbol(self):
        rows = gt.compare(self.conn, REGISTRY, {"J1": {("app/dyn.py", "stray")}})
        text = gt.render(rows, self.conn, unmapped=[])
        self.assertIn("SILENT-MISS SOURCE", text)
        self.assertIn("stray", text)
        self.assertIn("NO TRACE", text)

    def test_a_measurement_that_measured_nothing_reports_no_miss_count(self):
        # The old headline read "0/8 journey(s) traced; 0 traced symbol(s)
        # outside the static footprint" — the sentence a reader quotes, and
        # indistinguishable from a clean result.
        rows = gt.compare(self.conn, REGISTRY, {})
        text = gt.render(rows, self.conn, unmapped=["tests/test_a.py::t"])
        self.assertIn("NO MEASUREMENT", text)
        self.assertNotIn("outside the static footprint", text)

    def test_unresolved_entries_are_named_rather_than_rendered_as_misses(self):
        # An empty footprint makes EVERY traced node "outside" it. Reporting
        # that as the selector's worst defect hides a renamed entry point or a
        # mismatched --db behind a wall of false silent misses.
        registry = {
            "journeys": {"J1": {"name": "gone", "entries": [{"name": "vanished"}]}}
        }
        rows = gt.compare(self.conn, registry, {"J1": {("app/svc.py", "mid")}})
        self.assertEqual(rows[0]["entries_resolved"], 0)
        text = gt.render(rows, self.conn, unmapped=[])
        self.assertIn("ENTRIES UNRESOLVED", text)
        self.assertNotIn("SILENT-MISS SOURCE", text)

    def test_the_footprint_line_states_how_many_entries_resolved(self):
        rows = gt.compare(self.conn, REGISTRY, {"J1": {("app/svc.py", "mid")}})
        self.assertIn("resolved entry symbol", gt.render(rows, self.conn, unmapped=[]))


class CollectorTest(unittest.TestCase):
    """`tgtrace`'s recording logic, driven without pytest."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        tgtrace._root = os.path.abspath(self.root)
        tgtrace._skip = ("/tests/",)
        tgtrace._seen_files.clear()
        tgtrace._current = None
        self.addCleanup(setattr, tgtrace, "_current", None)

    def _code(self, filename, qualname="fn"):
        return type("C", (), {"co_filename": filename, "co_qualname": qualname,
                              "co_name": qualname})()

    def test_nothing_is_recorded_outside_a_test_body(self):
        tgtrace._record(self._code(os.path.join(self.root, "app.py")))
        self.assertIsNone(tgtrace._current)

    def test_a_function_under_the_root_is_recorded(self):
        tgtrace._current = set()
        tgtrace._record(self._code(os.path.join(self.root, "app.py"), "handler"))
        self.assertEqual(tgtrace._current, {("app.py", "handler")})

    def test_a_function_outside_the_root_is_ignored(self):
        # Everything in site-packages and the stdlib runs during a test; only
        # the target's own source is the journey's footprint.
        tgtrace._current = set()
        tgtrace._record(self._code("/usr/lib/python3/json/decoder.py", "loads"))
        self.assertEqual(tgtrace._current, set())

    def test_the_test_files_themselves_are_skipped(self):
        tgtrace._current = set()
        tgtrace._record(self._code(os.path.join(self.root, "tests", "test_x.py")))
        self.assertEqual(tgtrace._current, set())

    def test_a_synthetic_frame_is_ignored(self):
        tgtrace._current = set()
        tgtrace._record(self._code("<string>", "exec"))
        self.assertEqual(tgtrace._current, set())

    def test_a_repo_living_under_a_tests_directory_still_traces(self):
        # The skip list is matched against the path RELATIVE to the root. Match
        # it absolutely and a checkout at /tmp/pytest-of-me/test_0/repo skips
        # its own entire source tree, succeeds, and writes an empty trace.
        root = os.path.join(tempfile.mkdtemp(), "tests", "repo")
        os.makedirs(root)
        tgtrace._root = os.path.abspath(root)
        tgtrace._seen_files.clear()
        tgtrace._current = set()
        tgtrace._record(self._code(os.path.join(root, "app.py"), "handler"))
        self.assertEqual(tgtrace._current, {("app.py", "handler")})


if __name__ == "__main__":
    unittest.main()
