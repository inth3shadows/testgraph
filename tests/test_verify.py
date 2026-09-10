"""Phase 6. The module exists because inferred attribution was measured and
found untrustworthy (`harness/unit_vs_journey.py`: J1's 20 credited tests reach
14 symbols where a real invocation reaches 40, sharing 6), so attribution here
is DECLARED — and a declaration is an assertion, which is why most of this file
is about refusing to take one at face value.

The load-bearing test is `test_one_uncovered_journey_is_not_a_pass`. Its first
implementation returned OK whenever anything was credited, and on the first real
run printed `NO JOURNEY-LEVEL TEST: J1, J5, J6, J7` and exited 0 — four journeys
unverified behind a green from the other four.
"""
import os
import sqlite3
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph import results as res  # noqa: E402
from testgraph import verify  # noqa: E402


def build_conn():
    """J1 reaches its entry via `hook_main`; J2 via `select_entry`. `helper` is
    inside neither journey's entry set — it is what a mocked test touches."""
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
            ("fn:hook_main", "function", "hook_main", "hook_main", "app/hook.py", 1, 5),
            ("fn:select_entry", "function", "select_entry", "select_entry", "app/sel.py", 1, 5),
            ("fn:helper", "function", "helper", "helper", "app/hook.py", 7, 9),
        ],
    )
    conn.commit()
    return conn


REGISTRY = {
    "target": "demo",
    "journeys": {
        "J1": {"name": "hook", "entries": [{"name": "hook_main", "file": "app/hook.py"}]},
        "J2": {"name": "select", "entries": [{"name": "select_entry", "file": "app/sel.py"}]},
    },
}


def junit(**status_by_key):
    return {k: res.run_result("pytest", k, v) for k, v in status_by_key.items()}


class MarkerExpressionTests(unittest.TestCase):
    def test_uses_the_phase_3_tag_convention(self):
        """Not a second spelling of it. If this drifted from what
        `results.covers` produces, `-m` would silently select nothing and the
        run would look like a repo with no journey tests."""
        self.assertEqual(verify.marker_expression(["J2"]), "tg_J2")
        self.assertEqual(
            verify.marker_expression(["J1", "J2"]), "tg_J1 or tg_J2"
        )

    def test_the_expression_matches_what_covers_declares(self):
        template = res.DEFAULT_TAG_CONVENTIONS["pytest"]
        self.assertEqual(
            verify.marker_expression(["J4"]), res.format_tag(template, "J4")
        )


class ValidationTests(unittest.TestCase):
    """The level-1 gate: a declaration must be supported by the test's own
    trace. It cannot catch a test that enters the journey and then mocks
    everything beneath it — that is `harness/unit_vs_journey.py`'s job, and
    conflating the two would let this module claim a guarantee it cannot
    make."""

    def setUp(self):
        self.conn = build_conn()

    def test_a_supported_declaration_is_credited(self):
        trace = {
            "declared": {"tests/t.py::test_a": ["J2"]},
            "tests": {"tests/t.py::test_a": [["app/sel.py", "select_entry"]]},
        }
        credited, unvalidated, per_journey = verify.validate(
            self.conn, REGISTRY, trace, junit(**{"tests.t.test_a": "pass"})
        )
        self.assertEqual(credited, {"J2": "pass"})
        self.assertEqual(unvalidated, [])
        self.assertEqual(per_journey["J2"], ["tests/t.py::test_a"])

    def test_a_declaration_the_trace_does_not_support_is_refused(self):
        """A test claiming J1 whose trace never enters `hook_main`. Crediting it
        would let a marker assert a journey was verified when nothing ran it —
        the same confident green as inferred attribution, just hand-written."""
        trace = {
            "declared": {"tests/t.py::test_b": ["J1"]},
            "tests": {"tests/t.py::test_b": [["app/hook.py", "helper"]]},
        }
        credited, unvalidated, per_journey = verify.validate(
            self.conn, REGISTRY, trace, junit(**{"tests.t.test_b": "pass"})
        )
        self.assertEqual(credited, {})
        self.assertEqual(unvalidated, [("tests/t.py::test_b", "J1")])
        self.assertNotIn("J1", per_journey)

    def test_a_test_that_never_ran_a_body_neither_credits_nor_accuses(self):
        """Collected and declared, but skipped or errored in setup. No evidence
        either way — calling that a false declaration would punish a skip."""
        trace = {"declared": {"tests/t.py::test_c": ["J2"]}, "tests": {}}
        credited, unvalidated, _ = verify.validate(
            self.conn, REGISTRY, trace, junit(**{"tests.t.test_c": "skip"})
        )
        self.assertEqual(credited, {})
        self.assertEqual(unvalidated, [])

    def test_one_failing_declared_test_outweighs_passing_ones(self):
        trace = {
            "declared": {"tests/t.py::a": ["J2"], "tests/t.py::b": ["J2"]},
            "tests": {
                "tests/t.py::a": [["app/sel.py", "select_entry"]],
                "tests/t.py::b": [["app/sel.py", "select_entry"]],
            },
        }
        credited, _, per_journey = verify.validate(
            self.conn, REGISTRY, trace,
            junit(**{"tests.t.a": "pass", "tests.t.b": "fail"}),
        )
        self.assertEqual(credited, {"J2": "fail"})
        self.assertEqual(len(per_journey["J2"]), 2)

    def test_an_error_status_reaches_the_journey_as_a_failure(self):
        trace = {
            "declared": {"tests/t.py::a": ["J2"]},
            "tests": {"tests/t.py::a": [["app/sel.py", "select_entry"]]},
        }
        credited, _, _ = verify.validate(
            self.conn, REGISTRY, trace, junit(**{"tests.t.a": "error"})
        )
        self.assertEqual(credited, {"J2": "fail"})


class ExitCodeTests(unittest.TestCase):
    def test_one_uncovered_journey_is_not_a_pass(self):
        """THE test in this file. Selecting 8 journeys, verifying 4 and exiting
        0 is a partial answer wearing a full answer's exit code — and the exit
        code is what a CI job reads, not the paragraph above it."""
        self.assertEqual(
            verify.exit_code({
                "journeys": ["J1", "J2"],
                "credited": {"J2": "pass"},
                "uncovered": ["J1"],
            }),
            verify.EXIT_INCOMPLETE,
        )

    def test_every_selected_journey_covered_and_passing_is_ok(self):
        self.assertEqual(
            verify.exit_code({
                "journeys": ["J2"], "credited": {"J2": "pass"}, "uncovered": [],
            }),
            verify.EXIT_OK,
        )

    def test_a_failing_journey_beats_an_incomplete_one(self):
        """A real failure is more actionable than a coverage gap, and reporting
        the gap instead would bury it."""
        self.assertEqual(
            verify.exit_code({
                "journeys": ["J1", "J2"],
                "credited": {"J2": "fail"},
                "uncovered": ["J1"],
            }),
            verify.EXIT_TESTS_FAILED,
        )

    def test_nothing_selected_is_ok(self):
        self.assertEqual(
            verify.exit_code({"journeys": [], "credited": {}, "uncovered": []}),
            verify.EXIT_OK,
        )

    def test_a_refused_run_has_its_own_code(self):
        self.assertEqual(
            verify.exit_code({"refused": True}), verify.EXIT_REFUSED
        )


class SummaryShapeTests(unittest.TestCase):
    """Every exit from `run` carries the same keys.

    The three returns used to build their dicts independently and the two short
    ones omitted `repo`, so a NONE answer rendered `testgraph verify[?]`.
    Cosmetic in the text, not in `--json`: a consumer reading `summary["repo"]`
    got a KeyError on exactly the quiet runs it is most likely aggregating."""

    KEYS = {"repo", "commit", "selection", "journeys", "marker_expression",
            "collected", "credited", "unvalidated", "per_journey", "uncovered",
            "rows_written", "pytest_exit"}

    def test_the_empty_summary_carries_every_key(self):
        got = verify._summary("/nonexistent", "HEAD", {"base": "a", "head": "b"})
        self.assertTrue(self.KEYS <= set(got), self.KEYS - set(got))

    def test_a_refused_summary_carries_them_too(self):
        got = verify._summary("/nonexistent", "HEAD", {}, refused=True)
        self.assertTrue(self.KEYS <= set(got), self.KEYS - set(got))
        self.assertTrue(got["refused"])

    def test_extras_override_the_defaults(self):
        got = verify._summary("/nonexistent", "HEAD", {}, journeys=["J2"],
                              collected=3)
        self.assertEqual(got["journeys"], ["J2"])
        self.assertEqual(got["collected"], 3)

    def test_the_repo_name_reaches_a_none_answer(self):
        summary = verify._summary("/tmp/some-repo", "HEAD",
                                  {"base": "a", "head": "b", "warnings": []})
        out = verify.render(summary, REGISTRY)
        self.assertIn("some-repo", out)
        self.assertNotIn("[?]", out)


class RenderTests(unittest.TestCase):
    def test_uncovered_journeys_are_named_loudly_and_denied_a_pass(self):
        out = verify.render({
            "repo": "demo",
            "selection": {"base": "a", "head": "b", "warnings": []},
            "journeys": ["J1", "J2"],
            "marker_expression": "tg_J1 or tg_J2",
            "collected": 1,
            "credited": {"J2": "pass"},
            "per_journey": {"J2": ["tests/t.py::a"]},
            "uncovered": ["J1"],
            "unvalidated": [],
            "rows_written": 1,
            "pytest_exit": 0,
        }, REGISTRY)
        self.assertIn("NO JOURNEY-LEVEL TEST: J1", out)
        self.assertIn("it is not a pass", out)

    def test_a_refused_run_says_no_tests_were_run(self):
        out = verify.render({
            "refused": True,
            "repo": "demo",
            "selection": {"blocking": ["index likely corrupt"]},
        })
        self.assertIn("REFUSED", out)
        self.assertIn("No tests were run", out)


class CoversTests(unittest.TestCase):
    """`results.covers` cannot be `@pytest.mark.*`: CI is `python3 -m unittest
    discover` on a stdlib-only checkout with no pytest installed, so importing
    pytest in a test module would break the runner the suite actually uses."""

    def test_it_sets_a_plain_attribute_and_nothing_else(self):
        @res.covers("J2", "J4")
        def f():
            pass
        self.assertEqual(res.declared_journeys(f), ("J2", "J4"))
        self.assertEqual(getattr(f, res.JOURNEY_ATTR), ("J2", "J4"))

    def test_an_undecorated_object_declares_nothing(self):
        self.assertEqual(res.declared_journeys(lambda: None), ())

    def test_declaring_no_journey_is_refused(self):
        """A bare `@covers()` reads as a declaration and asserts nothing."""
        with self.assertRaises(ValueError):
            res.covers()

    def test_it_works_on_a_class(self):
        @res.covers("J1")
        class C:
            pass
        self.assertEqual(res.declared_journeys(C), ("J1",))


if __name__ == "__main__":
    unittest.main()
