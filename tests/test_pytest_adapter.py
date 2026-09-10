"""Phase 4: the pytest adapter that turns one suite run into ledger rows.

The interesting failure modes are all in the JOIN, not the subprocess: the two
halves of a run arrive keyed differently (tgtrace by nodeid, JUnit by
classname+name), and every way that join can go wrong makes coverage look
SMALLER than it was. A recall-first tool that silently under-reports is the
one outcome this project exists to prevent, so each of those paths is asserted
to be REPORTED rather than swallowed.
"""
import os
import sqlite3
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph.results import covers  # noqa: E402
from testgraph import ledger  # noqa: E402
from testgraph import pytest_adapter as pa  # noqa: E402


def build_conn():
    """Two journeys' worth of graph: J1 reaches handler->helper, J2 is a
    separate island. Shaped like tests/test_core.py's fixture."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT);
        CREATE TABLE schema_versions(version INT, applied_at INT, note TEXT);
        INSERT INTO schema_versions VALUES (9, 0, 'fixture');
        """
    )
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("function:handler", "function", "handler", "handler", "app/a.py", 1, 5),
            ("function:helper", "function", "helper", "helper", "app/a.py", 7, 9),
            ("function:other", "function", "other", "other", "app/b.py", 1, 5),
            # same bare name as `helper`, different file -- must NOT collide
            ("function:helper_b", "function", "helper", "helper", "app/b.py", 7, 9),
        ],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,metadata,provenance) VALUES (?,?,?,?,?)",
        [("function:handler", "function:helper", "calls", None, None)],
    )
    conn.commit()
    return conn


REGISTRY = {
    "target": "demo",
    "journeys": {
        "J1": {"name": "a", "entries": [{"name": "handler", "file": "app/a.py"}]},
        "J2": {"name": "b", "entries": [{"name": "other", "file": "app/b.py"}]},
    },
}


class JoinKeyTests(unittest.TestCase):
    """The nodeid <-> JUnit key normalization. Both sides must land on the
    same string or every test silently loses its verdict."""

    def test_module_level_test(self):
        self.assertEqual(
            pa._join_key("tests/test_x.py::test_y"), "tests.test_x.test_y"
        )
        self.assertEqual(pa._junit_key("tests.test_x", "test_y"), "tests.test_x.test_y")

    def test_class_based_test_agrees_from_both_sides(self):
        # The case that makes reconstructing a nodeid FROM junit ambiguous:
        # "tests.test_x.Cls" gives no way to know where the module ends.
        self.assertEqual(
            pa._join_key("tests/test_x.py::Cls::test_y"), "tests.test_x.Cls.test_y"
        )
        self.assertEqual(
            pa._junit_key("tests.test_x.Cls", "test_y"), "tests.test_x.Cls.test_y"
        )

    def test_parametrized_test(self):
        self.assertEqual(
            pa._join_key("tests/test_x.py::test_y[a-1]"), "tests.test_x.test_y[a-1]"
        )
        self.assertEqual(
            pa._junit_key("tests.test_x", "test_y[a-1]"), "tests.test_x.test_y[a-1]"
        )


class ParseJunitTests(unittest.TestCase):
    XML = """<?xml version="1.0"?>
    <testsuites><testsuite name="pytest" tests="4">
      <testcase classname="tests.test_a" name="test_pass" time="0.01"/>
      <testcase classname="tests.test_a" name="test_fail" time="0.02">
        <failure message="assert 1 == 2">trace...</failure>
      </testcase>
      <testcase classname="tests.test_a" name="test_error" time="0.03">
        <error message="fixture blew up">trace...</error>
      </testcase>
      <testcase classname="tests.test_a" name="test_skip" time="0.0">
        <skipped message="no reason"/>
      </testcase>
    </testsuite></testsuites>"""

    def test_all_four_statuses(self):
        parsed = pa.parse_junit(self.XML)
        self.assertEqual(parsed["tests.test_a.test_pass"]["status"], "pass")
        self.assertEqual(parsed["tests.test_a.test_fail"]["status"], "fail")
        self.assertEqual(parsed["tests.test_a.test_error"]["status"], "error")
        self.assertEqual(parsed["tests.test_a.test_skip"]["status"], "skip")

    def test_error_stays_distinct_from_failure_on_the_raw_record(self):
        # Both collapse to `fail` downstream, but the raw record is the last
        # place a fixture explosion is distinguishable from a bad assertion.
        parsed = pa.parse_junit(self.XML)
        self.assertNotEqual(
            parsed["tests.test_a.test_error"]["status"],
            parsed["tests.test_a.test_fail"]["status"],
        )

    def test_message_and_duration_carried(self):
        parsed = pa.parse_junit(self.XML)
        self.assertEqual(parsed["tests.test_a.test_fail"]["message"], "assert 1 == 2")
        self.assertEqual(parsed["tests.test_a.test_fail"]["duration_s"], 0.02)

    def test_malformed_time_does_not_raise(self):
        parsed = pa.parse_junit(
            '<testsuites><testsuite><testcase classname="c" name="n" time="NaNsec"/>'
            "</testsuite></testsuites>"
        )
        self.assertIsNone(parsed["c.n"]["duration_s"])


class ResolveTracedTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_conn()

    def test_same_bare_name_in_two_files_does_not_collide(self):
        ids, _ = pa.resolve_traced(self.conn, [("app/a.py", "helper")])
        self.assertEqual(ids, {"function:helper"})

    def test_qualname_uses_last_segment(self):
        # codegraph indexes `Cls.method` under the bare name `method`.
        self.conn.execute(
            "INSERT INTO nodes VALUES "
            "('function:m','function','method','Cls.method','app/a.py',20,25)"
        )
        ids, _ = pa.resolve_traced(self.conn, [("app/a.py", "Cls.method")])
        self.assertEqual(ids, {"function:m"})

    def test_unresolvable_symbol_is_skipped_not_fatal(self):
        ids, amb = pa.resolve_traced(self.conn, [("app/a.py", "nope")])
        self.assertEqual(ids, set())
        self.assertEqual(amb, [])

    def test_ambiguity_is_reported_and_still_counted(self):
        # Two nodes, same bare name, same file: counted (dropping would shrink
        # coverage silently) but surfaced so a thin margin is not trusted blind.
        self.conn.execute(
            "INSERT INTO nodes VALUES "
            "('function:helper2','function','helper','helper','app/a.py',30,35)"
        )
        ids, amb = pa.resolve_traced(self.conn, [("app/a.py", "helper")])
        self.assertEqual(len(ids), 2)
        self.assertEqual(amb, [("app/a.py", "helper")])


# Journey-level: exercises the real trace-to-journey join the adapter exists for.
@covers("J8")
class AttributeTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_conn()
        self.entries = pa.journey_entries(self.conn, REGISTRY)

    def test_a_journey_is_its_entry_symbols(self):
        self.assertEqual(self.entries["J1"], {"function:handler"})
        self.assertEqual(self.entries["J2"], {"function:other"})

    def test_touching_a_dependency_does_not_attribute_the_journey(self):
        """The bug this module was first written with, pinned.

        `helper` is reachable FROM J1's entry, so it is in J1's dependency
        footprint. Attributing on that basis made one CLI's tests cover every
        journey in testgraph, because everything shares `ledger.py`. Running
        a journey's dependency is not running the journey."""
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::t": [["app/a.py", "helper"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="t"/></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["journeys"], {})
        self.assertEqual(out["tests_covering_no_journey"], 1)

    def test_failing_test_marks_its_journey_failed(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::test_one": [["app/a.py", "handler"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="test_one"><failure message="x"/></testcase>'
                "</testsuite></testsuites>"
            ),
        )
        self.assertEqual(out["journeys"], {"J1": "fail"})
        self.assertEqual(out["covering_tests"]["J1"], 1)

    def test_one_failure_outweighs_many_passes(self):
        verdicts = pa.parse_junit(
            '<testsuites><testsuite>'
            '<testcase classname="tests.test_a" name="ok1"/>'
            '<testcase classname="tests.test_a" name="ok2"/>'
            '<testcase classname="tests.test_a" name="bad"><failure message="x"/></testcase>'
            "</testsuite></testsuites>"
        )
        out = pa.attribute(
            self.conn,
            {
                "tests/test_a.py::ok1": [["app/a.py", "handler"]],
                "tests/test_a.py::ok2": [["app/a.py", "handler"]],
                "tests/test_a.py::bad": [["app/a.py", "handler"]],
            },
            self.entries,
            verdicts,
        )
        self.assertEqual(out["journeys"]["J1"], "fail")
        self.assertEqual(out["covering_tests"]["J1"], 3)

    def test_test_outside_every_footprint_covers_nothing(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::t": [["app/zzz.py", "unknown"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="t"/></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["journeys"], {})
        self.assertEqual(out["tests_covering_no_journey"], 1)

    def test_one_test_can_cover_two_journeys(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::t": [["app/a.py", "handler"], ["app/b.py", "other"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="t"/></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["journeys"], {"J1": "pass", "J2": "pass"})

    def test_traced_test_with_no_verdict_is_reported(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::ghost": [["app/a.py", "handler"]]},
            self.entries,
            {},
        )
        self.assertEqual(out["traced_without_verdict"], ["tests/test_a.py::ghost"])
        self.assertEqual(out["journeys"], {})

    def test_verdict_with_no_trace_is_reported(self):
        out = pa.attribute(
            self.conn,
            {},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="untraced"/></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["verdicts_without_trace"], ["tests.test_a.untraced"])

    def test_skip_only_journey_records_skip(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::s": [["app/a.py", "handler"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="s"><skipped message="m"/></testcase></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["journeys"], {"J1": "skip"})

    def test_error_status_reaches_the_ledger_as_fail(self):
        out = pa.attribute(
            self.conn,
            {"tests/test_a.py::e": [["app/a.py", "handler"]]},
            self.entries,
            pa.parse_junit(
                '<testsuites><testsuite><testcase classname="tests.test_a" '
                'name="e"><error message="m"/></testcase></testsuite></testsuites>'
            ),
        )
        self.assertEqual(out["journeys"], {"J1": "fail"})


class WriteOutcomesTests(unittest.TestCase):
    def test_rows_are_tagged_trace_provenance(self):
        rows = []
        written = pa.write_outcomes(
            "demo", "a" * 40,
            {"journeys": {"J1": "fail"}, "covering_tests": {"J1": 3}},
            append=lambda r: (rows.append(r), True)[1],
        )
        self.assertEqual(len(written), 1)
        self.assertEqual(rows[0]["edge_provenance"], pa.TRACE_PROVENANCE)
        self.assertEqual(rows[0]["verdict"], "fail")
        self.assertEqual(rows[0]["journey"], "J1")
        self.assertEqual(rows[0]["kind"], ledger.OUTCOME)

    def test_manual_rows_carry_no_provenance_field_at_all(self):
        # The asymmetry is load-bearing: absence means a human asserted it.
        manual = ledger.outcome_row("demo", "a" * 40, "J1", "fail")
        self.assertNotIn("edge_provenance", manual)

    def test_a_failed_append_is_not_counted_as_written(self):
        written = pa.write_outcomes(
            "demo", "a" * 40,
            {"journeys": {"J1": "fail"}, "covering_tests": {"J1": 1}},
            append=lambda r: False,
        )
        self.assertEqual(written, [])


class RunRefusalTests(unittest.TestCase):
    """`run` refuses loudly rather than writing rows it cannot stand behind."""

    def test_missing_registry_is_an_error_not_an_empty_run(self):
        summary, err = pa.run("/nonexistent/repo")
        self.assertIsNone(summary)
        self.assertIn("no journey registry", err)


class RenderTests(unittest.TestCase):
    SUMMARY = {
        "repo": "demo",
        "commit": "a" * 40,
        "pytest_exit": 0,
        "backend": "sys.monitoring",
        "journeys": {"J1": "pass"},
        "covering_tests": {"J1": 3},
        "rows_written": 1,
        "tests_covering_no_journey": 5,
        "ambiguous_symbols": [],
        "traced_without_verdict": [],
        "verdicts_without_trace": [],
    }

    def test_uncovered_journey_is_named_not_just_omitted(self):
        """A report listing only what it found reads as completeness. The gap
        is the actionable half -- measured on testgraph, J6 has zero covering
        tests in a 359-test suite and only this line says so."""
        out = pa.render(self.SUMMARY, REGISTRY)
        self.assertIn("NO COVERING TEST", out)
        self.assertIn("J2", out)

    def test_dry_run_does_not_claim_rows_were_written(self):
        dry = dict(self.SUMMARY, dry_run=True, rows_written=0)
        out = pa.render(dry, REGISTRY)
        self.assertIn("DRY RUN", out)
        self.assertNotIn("1 ledger row(s) written", out)

    def test_real_run_reports_what_it_wrote(self):
        out = pa.render(self.SUMMARY, REGISTRY)
        self.assertIn("1 ledger row(s) written", out)
        self.assertNotIn("DRY RUN", out)

    def test_incomplete_join_is_flagged_in_both_directions(self):
        noisy = dict(
            self.SUMMARY,
            traced_without_verdict=["tests/t.py::a"],
            verdicts_without_trace=["tests.t.b"],
            ambiguous_symbols=[("app/a.py", "helper")],
        )
        out = pa.render(noisy, REGISTRY)
        self.assertIn("coverage understated", out)
        self.assertIn("coverage may be overstated", out)


if __name__ == "__main__":
    unittest.main()
