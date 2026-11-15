"""Phase 4: the pytest adapter that turns one suite run into ledger rows.

The interesting failure modes are all in the JOIN, not the subprocess: the two
halves of a run arrive keyed differently (tgtrace by nodeid, JUnit by
classname+name), and every way that join can go wrong makes coverage look
SMALLER than it was. A recall-first tool that silently under-reports is the
one outcome this project exists to prevent, so each of those paths is asserted
to be REPORTED rather than swallowed.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph.results import covers  # noqa: E402
from testgraph import ledger  # noqa: E402
from testgraph import pytest_adapter as pa  # noqa: E402
from test_core import _db_on_disk, _git_repo, _registry_file, build_fixture  # noqa: E402


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

    def test_parametrized_id_containing_double_colon(self):
        # Audit H4: pytest splits off `[...]` BEFORE `::`, so a param id may
        # itself contain `::`. Splitting on `::` first shredded the key and the
        # test silently lost its verdict.
        self.assertEqual(
            pa._join_key("tests/t.py::test_p[a::b]"), "tests.t.test_p[a::b]"
        )
        self.assertEqual(pa._junit_key("tests.t", "test_p[a::b]"), "tests.t.test_p[a::b]")

    def test_class_parametrized_id_with_dot_is_unchanged(self):
        self.assertEqual(
            pa._join_key("tests/t.py::C::test_p[1.5]"), "tests.t.C.test_p[1.5]"
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



class RunTraceRefusalTests(unittest.TestCase):
    """#108 / #154: a broken run is refused, never attributed. Each case
    writes what a real pytest would have left in the work dir; `_workdir`
    stands in for the temp dir, so nothing patches the stdlib."""

    WHOLE = '{"tests": {}, "declared": {}}'
    JUNIT = "<testsuite/>"

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.db = _db_on_disk(tmp, build_fixture())
        self.registry = _registry_file(tmp, {
            "J1": {"name": "ok", "entries": [{"name": "handler_a", "file": "app/svc.py"}]},
        })
        self.repo, _ = _git_repo(tmp, {"app/svc.py": 22})
        self.work = os.path.join(tmp, "work")
        os.makedirs(self.work)

    def _run(self, trace=None, junit=None, stderr="", **kw):
        work = self.work

        def runner():
            for name, text in (("trace.json", trace), ("junit.xml", junit)):
                if text is not None:
                    with open(os.path.join(work, name), "w") as fh:
                        fh.write(text)
            return types.SimpleNamespace(returncode=0, stderr=stderr)

        return pa.run(self.repo, registry_path=self.registry, db_path=self.db,
                      _runner=runner, _workdir=work, append=lambda r: True, **kw)

    def test_refusals(self):
        cases = [
            ("truncated", dict(trace='{"tests": {', junit=JUNIT_OK), "truncated or not JSON"),
            ("not an object", dict(trace="[]", junit=JUNIT_OK), "not a tgtrace payload"),
            ("no trace", dict(junit=JUNIT_OK, stderr="boom: xdist"), "wrote no trace"),
            ("no junit", dict(trace=self.WHOLE), "wrote no JUnit XML"),
        ]
        for label, kw, expect in cases:
            with self.subTest(label):
                for f in os.listdir(self.work):
                    os.remove(os.path.join(self.work, f))
                summary, err = self._run(**kw)
                self.assertIsNone(summary)
                self.assertIn(expect, err)

    def test_the_trace_check_comes_before_the_junit_check(self):
        """#154: verify and the adapter refused in different orders. With
        both files missing, both now name the trace first."""
        summary, err = self._run(stderr="ERROR: unrecognized arguments: --bogus")
        self.assertIn("wrote no trace", err)
        self.assertIn("--bogus", err)

    def test_a_whole_run_attributes(self):
        summary, err = self._run(trace=self.WHOLE, junit=JUNIT_OK)
        self.assertIsNone(err)
        self.assertEqual(summary["rows_written"], 0)

    def test_trace_out_is_written_whole_and_leaves_no_temp_file(self):
        out_dir = os.path.join(self.tmp, "out")
        os.makedirs(out_dir)
        out = os.path.join(out_dir, "t.json")
        summary, err = self._run(trace=self.WHOLE, junit=JUNIT_OK, trace_out=out)
        self.assertIsNone(err)
        with open(out) as fh:
            self.assertEqual(json.load(fh), json.loads(self.WHOLE))
        self.assertEqual(os.listdir(out_dir), ["t.json"])


JUNIT_OK = RunTraceRefusalTests.JUNIT


class WriteJsonAtomicTests(unittest.TestCase):
    """#154: `--trace-out` was copied with a plain open-and-dump."""

    def test_a_dump_that_dies_leaves_neither_target_nor_temp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "t.json")
        # json.dump streams chunks, so the temp file is part-written when the
        # unserializable value is reached: the real mid-dump death, without
        # patching the json module for the whole process.
        with self.assertRaises(TypeError):
            pa.write_json_atomic(out, {"a": 1, "z": object()})
        self.assertEqual(os.listdir(tmp), [])

    def test_an_existing_target_survives_a_failed_write(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "t.json")
        pa.write_json_atomic(out, {"old": True})
        with self.assertRaises(TypeError):
            pa.write_json_atomic(out, {"z": object()})
        with open(out) as fh:
            self.assertEqual(json.load(fh), {"old": True})


class MainJsonRefusalTests(unittest.TestCase):
    def test_a_json_refusal_is_json(self):
        """#154: the refusal went to stderr as prose even under `--json`."""
        out = io.StringIO()
        with unittest.mock.patch.object(pa, "run", return_value=(None, "trace broke")), \
                contextlib.redirect_stdout(out):
            rc = pa.main(["--repo", "/x", "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue()), {"refused": True, "error": "trace broke"})



class ReviewRound1Tests(RunTraceRefusalTests):
    """Round-1 review of #154 (.review ledger F1, F3, F4, F8, F9)."""

    def test_f3_a_truncated_junit_report_is_refused_not_a_crash(self):
        for text in ("<testsuites><testsuite>", ""):
            with self.subTest(repr(text)):
                for f in os.listdir(self.work):
                    os.remove(os.path.join(self.work, f))
                summary, err = self._run(trace=self.WHOLE, junit=text)
                self.assertIsNone(summary)
                self.assertIn("JUnit XML is truncated or not XML", err)

    def test_f4_an_object_without_tgtrace_fields_is_refused(self):
        for text in ("{}", '{"tests": 3}', '{"tests": {}, "declared": 3}',
                     '{"tests": {"t": 3}}'):
            with self.subTest(text):
                for f in os.listdir(self.work):
                    os.remove(os.path.join(self.work, f))
                summary, err = self._run(trace=text, junit=JUNIT_OK)
                self.assertIsNone(summary)
                self.assertIn("not a tgtrace payload", err)

    def test_f9_a_trace_out_that_cannot_be_written_is_refused_before_the_run(self):
        ran = []
        bad = [os.path.join(self.tmp, "no", "such", "t.json"), self.tmp]
        for out in bad:
            with self.subTest(out):
                def runner():
                    ran.append(out)
                    return types.SimpleNamespace(returncode=0, stderr="")
                summary, err = pa.run(self.repo, registry_path=self.registry,
                                      db_path=self.db, _runner=runner,
                                      _workdir=self.work, append=lambda r: True,
                                      trace_out=out)
                self.assertIsNone(summary)
                self.assertIn("--trace-out", err)
        self.assertEqual(ran, [], "the suite ran before --trace-out was checked")

    def test_f1_trace_out_to_a_fifo_is_written_through(self):
        fifo = os.path.join(self.tmp, "pipe")
        os.mkfifo(fifo)
        got = []
        import threading
        t = threading.Thread(target=lambda: got.append(open(fifo).read()), daemon=True)
        t.start()
        summary, err = self._run(trace=self.WHOLE, junit=JUNIT_OK, trace_out=fifo)
        t.join(5)
        self.assertIsNone(err)
        self.assertEqual(json.loads(got[0]), json.loads(self.WHOLE))
        self.assertTrue(__import__("stat").S_ISFIFO(os.stat(fifo).st_mode))


class WriterParityTests(unittest.TestCase):
    """F1/F2/F8: testgraph's atomic writer and tgtrace's are two copies of one
    rule (tgtrace must stay standalone, so it cannot import the other). This
    holds them to the same behaviour rather than a comment saying they match."""

    def _writers(self):
        sys.path.insert(0, os.path.join(ROOT_DIR, "harness", "plugin"))
        try:
            import tgtrace
        finally:
            sys.path.pop(0)
        return {"adapter": pa.write_json_atomic, "tgtrace": tgtrace._write}

    def test_both_treat_dev_paths_as_streams(self):
        """/dev/stdout redirected to a file resolves to a regular file, so an
        `isfile` test alone sends it down the temp-and-rename path, which
        cannot create a file in /dev (F2)."""
        sys.path.insert(0, os.path.join(ROOT_DIR, "harness", "plugin"))
        import tgtrace
        sys.path.pop(0)
        for path in ("/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"):
            with self.subTest(path):
                self.assertTrue(pa.is_stream(path))
                self.assertTrue(tgtrace._is_stream(path))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        regular = os.path.join(tmp, "t.json")
        open(regular, "w").close()
        for path in (regular, os.path.join(tmp, "absent.json")):
            self.assertFalse(pa.is_stream(path))
            self.assertFalse(tgtrace._is_stream(path))

    def test_both_write_dev_stdout_redirected_to_a_file(self):
        """F2, end to end: a child whose stdout IS a regular file writes
        through /dev/stdout with each writer."""
        import subprocess
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, code in (
            ("adapter", "from testgraph import pytest_adapter as w; w.write_json_atomic('/dev/stdout', {'ok': 1})"),
            ("tgtrace", "import sys; sys.path.insert(0, 'harness/plugin'); import tgtrace as w; w._write('/dev/stdout', {'ok': 1})"),
        ):
            with self.subTest(name):
                out = os.path.join(tmp, name + ".json")
                with open(out, "w") as fh:
                    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT_DIR,
                                          stdout=fh, stderr=subprocess.PIPE, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(out) as fh:
                    self.assertEqual(json.load(fh), {"ok": 1})

    def test_both_name_their_temp_file_the_same_way(self):
        """F8: the harness sweep matches `<out>.tmp.<pid>`; a killed adapter
        used to leave `.<name>.tmp.<pid>`, which nothing swept."""
        for name, write in self._writers().items():
            with self.subTest(name):
                tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                out = os.path.join(tmp, "t.json")
                seen = []
                real_replace = os.replace

                def spy(src, dst):
                    seen.append(os.path.basename(src))
                    return real_replace(src, dst)
                with unittest.mock.patch("os.replace", spy):
                    write(out, {"a": 1})
                self.assertEqual(seen, [f"t.json.tmp.{os.getpid()}"])


if __name__ == "__main__":
    unittest.main()
