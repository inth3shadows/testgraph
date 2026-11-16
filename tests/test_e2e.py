"""`verify --runner playwright`: Playwright tests declared for the selected journeys (#122).

The runner is faked with recorded Playwright JSON reports, in the shape a real
run produced against honeyslate (specs carry `tags` without the '@'; an untagged
setup project sits beside the journey tests). CI has no browser; the real run is
documented in TECHNICAL.md.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)
from testgraph import e2e  # noqa: E402
from testgraph import verify  # noqa: E402
from test_core import _db_on_disk, _git_repo, build_fixture  # noqa: E402


def spec(title, tags, status, last="passed"):
    results = [] if status == "skipped" else [{"status": last}]
    if status == "flaky":
        results = [{"status": "failed"}, {"status": "passed"}]
    return {"title": title, "tags": tags,
            "tests": [{"projectName": "journeys", "status": status,
                       "results": results}]}


def report(*specs, errors=()):
    return {"suites": [{"title": "j.spec.js", "specs": list(specs), "suites": []}],
            "errors": list(errors), "stats": {}}


SETUP_OK = spec("seed and log in", [], "expected")


class GrepTests(unittest.TestCase):
    def test_one_journey_does_not_select_its_longer_namesake(self):
        expr = e2e.grep_expression(["J1"])
        self.assertTrue(re.search(expr, "adds a task @tg:J1"))
        self.assertTrue(re.search(expr, "adds a task @tg:J1 @slow"))
        self.assertFalse(re.search(expr, "other journey @tg:J10"))

    def test_several_journeys_are_a_union(self):
        expr = e2e.grep_expression(["J1", "J2"])
        self.assertTrue(re.search(expr, "x @tg:J2"))
        self.assertFalse(re.search(expr, "x @tg:J3"))

    def test_journey_ids_are_matched_literally(self):
        expr = e2e.grep_expression(["J1.2"])
        self.assertTrue(re.search(expr, "x @tg:J1.2"))
        self.assertFalse(re.search(expr, "x @tg:J1x2"))

    def test_the_tag_follows_the_project_convention(self):
        self.assertEqual(e2e.tag("J1"), "@tg:J1")

    def test_P5_the_grep_argument_is_case_sensitive(self):
        """Playwright compiles a bare --grep with flags "gi"; a /.../ literal
        with no flags is compiled as written (forceRegExp, lib/util.js)."""
        self.assertEqual(e2e.grep_argument(["J1"]), "/" + e2e.grep_expression(["J1"]) + "/")


class OwnedFlagTests(unittest.TestCase):
    def test_P4_flags_verify_owns_are_found(self):
        for args in (("--grep", "x"), ("--grep=x",), ("-g", "x"), ("-gx",), ("-xg", "x"),
                     ("--grep-invert", "x"), ("--grep-invert=x",), ("--reporter=list",),
                     ("--reporter", "list"), ("-c", "x"), ("--config=x",), ("-cx",)):
            with self.subTest(args):
                self.assertEqual(e2e.owned_flag(args), args[0])

    def test_P4_other_flags_pass(self):
        for args in ((), ("--workers=1",), ("-j", "2"), ("-j2",), ("-x",), ("--project", "journeys"),
                     ("--headed",), ("--shard=1/2",), ("tests/a.spec.js",), ("-u",),
                     ("--max-failures=1",), ("--retries", "1")):
            with self.subTest(args):
                self.assertIsNone(e2e.owned_flag(args))


class JudgeTests(unittest.TestCase):
    def _judge(self, *specs, journeys=("J1",)):
        per, untagged, unknown = e2e.parse_report(report(SETUP_OK, *specs), list(journeys))
        return e2e.judge(per) + (untagged, unknown)

    def test_a_passing_tagged_test_is_a_pass(self):
        credited, incomplete, uncovered, _, _ = self._judge(spec("a", ["tg:J1"], "expected"))
        self.assertEqual((credited, incomplete, uncovered), ({"J1": "pass"}, [], []))

    def test_one_failure_fails_the_journey(self):
        credited, *_ = self._judge(spec("a", ["tg:J1"], "expected"),
                                   spec("b", ["tg:J1"], "unexpected", "failed"))
        self.assertEqual(credited, {"J1": "fail"})

    def test_a_pass_only_on_retry_is_not_a_pass(self):
        credited, incomplete, *_ = self._judge(spec("a", ["tg:J1"], "flaky"))
        self.assertEqual(credited, {})
        self.assertEqual(incomplete, [("J1", "flaky")])

    def test_skipped_only_is_not_a_pass(self):
        credited, incomplete, *_ = self._judge(spec("a", ["tg:J1"], "skipped"))
        self.assertEqual(credited, {"J1": "skip"})
        self.assertEqual(incomplete, [("J1", "skip")])

    def test_an_expected_failure_is_not_a_pass(self):
        """`test.fail()` reports `expected` with a failed attempt."""
        credited, incomplete, *_ = self._judge(spec("a", ["tg:J1"], "expected", "failed"))
        self.assertNotEqual(credited.get("J1"), "pass")
        # P7: not "skip" — the test ran and failed as test.fail() expected.
        self.assertEqual(credited, {})
        self.assertEqual(incomplete, [("J1", "expected-fail")])

    def test_P5_a_failure_tagged_only_for_other_journeys_is_a_broken_environment(self):
        for tags in (["tg:J2"], ["tg:j1"], ["TG:J1"]):
            with self.subTest(tags):
                _, untagged, _ = e2e.parse_report(
                    report(spec("setup", tags, "unexpected", "failed"),
                           spec("a", ["tg:J1"], "skipped")), ["J1"])
                self.assertEqual(untagged, ["setup"])

    def test_no_tagged_test_is_uncovered(self):
        credited, incomplete, uncovered, *_ = self._judge(spec("a", ["tg:J2"], "expected"))
        self.assertEqual(uncovered, ["J1"])

    def test_a_failing_untagged_test_is_reported(self):
        per, untagged, _ = e2e.parse_report(
            report(spec("seed and log in", [], "unexpected", "failed"),
                   spec("a", ["tg:J1"], "skipped")), ["J1"])
        self.assertEqual(untagged, ["seed and log in"])

    def test_other_tags_are_ignored_and_unknown_journeys_named(self):
        _, _, uncovered, _, unknown = self._judge(
            spec("a", ["tg:J1", "slow"], "expected"), spec("b", ["tg:J9"], "expected"))
        self.assertEqual(uncovered, [])
        self.assertEqual(unknown, ["J9"])


class RunE2ETests(unittest.TestCase):
    """verify.run_e2e over a real git diff that selects J1, with a fake runner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = build_fixture()
        self.db = _db_on_disk(self.tmp, src)
        src.close()
        self.repo, run = _git_repo(self.tmp, {"app/svc.py": 22})
        os.makedirs(os.path.join(self.repo, "frontend"))
        full = os.path.join(self.repo, "app", "svc.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[11] = "changed = 1\n"  # inside handler_a -> J1
        with open(full, "w") as fh:
            fh.writelines(lines)
        run("git", "add", "-A")
        run("git", "commit", "-qm", "touch")
        self.registry = self._registry({"runner": "playwright", "cwd": "frontend",
                                        "config": "playwright.real.config.js"})
        self.rows = []

    def _registry(self, e2e_block):
        path = os.path.join(self.tmp, "reg.json")
        body = {"codegraph_schema_version": 8, "approved": True, "spot_checks": {},
                "journeys": {"J1": {"name": "submit", "entries": [
                    {"name": "handler_a", "file": "app/svc.py"}]}}}
        if e2e_block is not None:
            body["e2e"] = e2e_block
        with open(path, "w") as fh:
            json.dump(body, fh)
        return path

    def _run(self, rep, returncode=0, write=True, stderr=""):
        seen = {}

        def runner(cmd, cwd, env):
            seen.update(cmd=cmd, cwd=cwd)
            if write:
                with open(env["PLAYWRIGHT_JSON_OUTPUT_NAME"], "w") as fh:
                    fh.write(rep if isinstance(rep, str) else json.dumps(rep))
            return types.SimpleNamespace(returncode=returncode, stderr=stderr)

        summary, err = verify.run_e2e(self.repo, "HEAD~1", "HEAD",
                                      registry_path=self.registry, db_path=self.db,
                                      append=lambda row: self.rows.append(row) or True,
                                      _runner=runner)
        return summary, err, seen

    def test_a_pass_is_exit_0_and_labelled_declared(self):
        summary, err, seen = self._run(report(SETUP_OK, spec("a", ["tg:J1"], "expected")))
        self.assertIsNone(err, err)
        self.assertEqual(summary["credited"], {"J1": "pass"})
        self.assertEqual(verify.exit_code(summary), verify.EXIT_OK)
        out = verify.render(summary)
        self.assertIn("DECLARED, not trace-validated", out)
        self.assertIn("edge_provenance=e2e-declared", out)
        self.assertEqual([r["edge_provenance"] for r in self.rows], ["e2e-declared"])
        self.assertEqual(seen["cwd"], os.path.join(self.repo, "frontend"))
        self.assertEqual(seen["cmd"][:7], ["npx", "--no-install", "playwright", "test",
                                           "-c", "playwright.real.config.js", "--grep"])

    def test_a_failure_is_exit_1(self):
        summary, err, _ = self._run(
            report(SETUP_OK, spec("a", ["tg:J1"], "unexpected", "failed")), returncode=1)
        self.assertIsNone(err, err)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_TESTS_FAILED)

    def test_no_tagged_test_is_exit_3(self):
        summary, err, _ = self._run(report(SETUP_OK))
        self.assertIsNone(err, err)
        self.assertEqual(summary["uncovered"], ["J1"])
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)
        self.assertIn("no Playwright test in this run carries a `@tg:<id>` tag", verify.render(summary))

    def test_a_flaky_pass_is_exit_3(self):
        summary, err, _ = self._run(report(SETUP_OK, spec("a", ["tg:J1"], "flaky")))
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)
        self.assertIn("passed only on a retry", verify.render(summary))
        self.assertEqual(self.rows, [])

    def test_a_skipped_only_journey_writes_no_ledger_row(self):
        summary, err, _ = self._run(report(SETUP_OK, spec("a", ["tg:J1"], "skipped")))
        self.assertIsNone(err, err)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)
        self.assertEqual(self.rows, [])

    def test_a_nonzero_runner_exit_with_nothing_failed_is_not_a_pass(self):
        summary, err, _ = self._run(report(SETUP_OK, spec("a", ["tg:J1"], "expected")),
                                    returncode=1)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)

    def test_a_broken_setup_is_refused(self):
        summary, err, _ = self._run(report(spec("seed and log in", [], "unexpected", "failed"),
                                           spec("a", ["tg:J1"], "skipped")), returncode=1)
        self.assertIsNone(summary)
        self.assertIn("untagged Playwright test failed", err)

    def test_no_report_is_refused_with_stderr(self):
        summary, err, _ = self._run(None, returncode=1, write=False,
                                    stderr="Error: browserType.launch: Executable doesn't exist")
        self.assertIsNone(summary)
        self.assertIn("wrote no JSON report", err)
        self.assertIn("Executable doesn't exist", err)

    def test_a_run_error_or_unreadable_report_is_refused(self):
        for rep, want in ((report(errors=[{"message": "config: no tests"}]), "run error"),
                          ("{", "unreadable"), ("[]", "not a JSON object")):
            with self.subTest(want):
                summary, err, _ = self._run(rep)
                self.assertIsNone(summary)
                self.assertIn(want, err)

    def test_P1_a_malformed_e2e_block_is_refused_not_a_crash(self):
        for block in ("playwright", 5, True, ["x"],
                      {"runner": "playwright", "cwd": None},
                      {"runner": "playwright", "cwd": 5},
                      {"runner": "playwright"},
                      {"runner": "playwright", "cwd": ""},
                      {"runner": "playwright", "cwd": "frontend", "config": 5},
                      {"runner": "playwright", "cwd": "frontend", "config": ["a"]}):
            with self.subTest(block):
                self.registry = self._registry(block)
                summary, err, _ = self._run(report())
                self.assertIsNone(summary)
                self.assertIn("registry e2e", err)

    def test_P3_a_cwd_or_config_outside_the_repo_is_refused(self):
        out = os.path.join(self.tmp, "outside")
        os.makedirs(out)
        os.symlink(out, os.path.join(self.repo, "frontend", "escape"))
        for block in ({"runner": "playwright", "cwd": "../.."},
                      {"runner": "playwright", "cwd": "frontend/../.."},
                      {"runner": "playwright", "cwd": self.tmp},
                      {"runner": "playwright", "cwd": "frontend/escape"},
                      {"runner": "playwright", "cwd": "frontend", "config": "-x"},
                      {"runner": "playwright", "cwd": "frontend", "config": "/etc/x.js"},
                      {"runner": "playwright", "cwd": "frontend", "config": "../../x.js"},
                      {"runner": "playwright", "cwd": "frontend", "config": "escape/x.js"}):
            with self.subTest(block):
                self.registry = self._registry(block)
                summary, err, seen = self._run(report())
                self.assertIsNone(summary)
                self.assertIn("registry e2e", err)
                self.assertEqual(seen, {})  # nothing ran

    def test_P3_the_repo_root_itself_is_a_valid_cwd(self):
        self.registry = self._registry({"runner": "playwright", "cwd": "."})
        summary, err, seen = self._run(report(SETUP_OK))
        self.assertIsNone(err, err)
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.repo))

    def test_P12_a_bad_block_is_refused_even_when_nothing_is_selected(self):
        self.registry = self._registry({"runner": "playwright", "cwd": "../.."})
        summary, err = verify.run_e2e(self.repo, "HEAD", "HEAD", registry_path=self.registry,
                                      db_path=self.db, append=lambda r: True,
                                      _runner=lambda *a: None)
        self.assertIsNone(summary)
        self.assertIn("registry e2e", err)

    def test_P2_no_matching_test_is_not_an_error(self):
        _, _, seen = self._run(report(SETUP_OK))
        self.assertIn("--pass-with-no-tests", seen["cmd"])

    def test_P5_the_command_greps_case_sensitively(self):
        _, _, seen = self._run(report(SETUP_OK))
        self.assertEqual(seen["cmd"][seen["cmd"].index("--grep") + 1], e2e.grep_argument(["J1"]))

    def test_P4_a_flag_verify_owns_is_refused_before_the_run(self):
        seen = {}
        summary, err = verify.run_e2e(self.repo, "HEAD~1", "HEAD", registry_path=self.registry,
                                      db_path=self.db, runner_args=("-g", "smoke"),
                                      append=lambda r: True,
                                      _runner=lambda *a: seen.update(ran=True))
        self.assertIsNone(summary)
        self.assertIn("owns", err)
        self.assertEqual(seen, {})

    def test_P4_e2e_run_refuses_an_owned_flag_itself(self):
        fields, err = e2e.run(self.repo, ["J1"], {"runner": "playwright", "cwd": "frontend"},
                              extra_args=("--reporter=list",),
                              _runner=lambda *a: self.fail("must not run"))
        self.assertIsNone(fields)
        self.assertIn("owns", err)

    def test_P4_an_owned_flag_is_refused_even_when_nothing_is_selected(self):
        summary, err = verify.run_e2e(self.repo, "HEAD", "HEAD", registry_path=self.registry,
                                      db_path=self.db, runner_args=("--grep", "x"),
                                      append=lambda r: True, _runner=lambda *a: None)
        self.assertIsNone(summary)
        self.assertIn("owns", err)

    def test_P4_an_inherited_json_output_file_cannot_redirect_the_report(self):
        with unittest.mock.patch.dict(os.environ, {
                "PLAYWRIGHT_JSON_OUTPUT_FILE": os.path.join(self.tmp, "theirs.json")}):
            captured = {}

            def runner(cmd, cwd, env):
                captured.update(env)
                with open(env["PLAYWRIGHT_JSON_OUTPUT_FILE"], "w") as fh:
                    json.dump(report(SETUP_OK, spec("a", ["tg:J1"], "expected")), fh)
                return types.SimpleNamespace(returncode=0, stderr="")
            summary, err = verify.run_e2e(self.repo, "HEAD~1", "HEAD",
                                          registry_path=self.registry, db_path=self.db,
                                          append=lambda r: True, _runner=runner)
        self.assertIsNone(err, err)
        self.assertEqual(captured["PLAYWRIGHT_JSON_OUTPUT_FILE"],
                         captured["PLAYWRIGHT_JSON_OUTPUT_NAME"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "theirs.json")))

    def test_P6_a_run_stopped_by_max_failures_still_reports_the_failure(self):
        summary, err, _ = self._run(report(
            SETUP_OK, spec("a", ["tg:J1"], "unexpected", "failed"),
            errors=[{"message": "Testing stopped early after 1 maximum allowed failures."}]),
            returncode=1)
        self.assertIsNone(err, err)
        self.assertEqual(summary["credited"], {"J1": "fail"})
        self.assertEqual(verify.exit_code(summary), verify.EXIT_TESTS_FAILED)

    def test_P6_a_stopped_run_with_no_tagged_failure_is_still_refused(self):
        summary, err, _ = self._run(report(
            SETUP_OK, spec("a", ["tg:J1"], "skipped"),
            errors=[{"message": "Testing stopped early after 1 maximum allowed failures."}]),
            returncode=1)
        self.assertIsNone(summary)
        self.assertIn("run error", err)

    def test_P7_an_expected_failure_renders_as_such(self):
        summary, err, _ = self._run(report(SETUP_OK, spec("a", ["tg:J1"], "expected", "failed")))
        self.assertIsNone(err, err)
        self.assertEqual(summary["incomplete"], [("J1", "expected-fail")])
        text = verify.render(summary)
        self.assertIn("failed as test.fail() expected", text)
        self.assertNotIn("(skipped)", text)

    def test_a_registry_without_e2e_is_refused(self):
        self.registry = self._registry(None)
        summary, err, _ = self._run(report())
        self.assertIsNone(summary)
        self.assertIn("no `e2e` block", err)

    def test_an_unknown_runner_is_refused(self):
        self.registry = self._registry({"runner": "cypress", "cwd": "frontend"})
        summary, err, _ = self._run(report())
        self.assertIsNone(summary)
        self.assertIn("e2e.runner", err)

    def test_runner_playwright_routes_to_e2e(self):
        with unittest.mock.patch.object(verify, "run_e2e",
                                        return_value=(None, "e2e path")) as m, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = verify.main(["--repo", self.repo, "--base", "HEAD~1", "--runner", "playwright",
                              "--", "--workers=1"])
        self.assertEqual(rc, verify.EXIT_REFUSED)
        self.assertEqual(m.call_args.kwargs["runner_args"], ("--workers=1",))


PW_NODE_MODULES = os.environ.get(
    "TG_PLAYWRIGHT_NODE_MODULES",
    "/home/ericm/personal_projects/honeyslate/e2e-j1-real/frontend/node_modules")
HAVE_PLAYWRIGHT = bool(shutil.which("node")) and os.path.isfile(
    os.path.join(PW_NODE_MODULES, "@playwright", "test", "package.json"))
PW_CONFIG = ('const { defineConfig } = require("@playwright/test");\n'
             'module.exports = defineConfig({ testDir: ".", workers: 1 });\n')
PW_HEAD = 'const { test, expect } = require("@playwright/test");\n'


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs node and a Playwright install "
                     "(TG_PLAYWRIGHT_NODE_MODULES)")
class RealPlaywrightVerifyTests(unittest.TestCase):
    """verify --runner playwright against real Playwright 1.x: tests with no
    `page`, so no browser starts. One commit selects J1."""

    setUp = RunE2ETests.setUp
    _registry = RunE2ETests._registry

    def _real(self, body, args=()):
        fe = os.path.join(self.repo, "frontend")
        if not os.path.lexists(os.path.join(fe, "node_modules")):
            os.symlink(PW_NODE_MODULES, os.path.join(fe, "node_modules"))
            with open(os.path.join(fe, "pw.config.js"), "w") as fh:
                fh.write(PW_CONFIG)
        with open(os.path.join(fe, "a.spec.js"), "w") as fh:
            fh.write(PW_HEAD + body)
        self.registry = self._registry({"runner": "playwright", "cwd": "frontend",
                                        "config": "pw.config.js"})
        summary, err = verify.run_e2e(self.repo, "HEAD~1", "HEAD",
                                      registry_path=self.registry, db_path=self.db,
                                      runner_args=args, append=lambda r: True)
        self.assertIsNone(err, err)
        self.assertEqual(summary["journeys"], ["J1"])
        return summary

    def test_a_tagged_pass_is_exit_0_and_untagged_tests_do_not_run(self):
        s = self._real('test("adds @tg:J1", () => {});\n'
                       'test("other", () => { throw new Error("must not run"); });\n')
        self.assertEqual(s["credited"], {"J1": "pass"})
        self.assertEqual((s["runner_exit"], verify.exit_code(s)), (0, verify.EXIT_OK))

    def test_P2_no_tagged_test_is_uncovered_not_refused(self):
        s = self._real('test("someone else @tg:J9", () => {});\n')
        self.assertEqual(s["uncovered"], ["J1"])
        self.assertEqual(verify.exit_code(s), verify.EXIT_INCOMPLETE)

    def test_P5_a_lowercase_tag_is_not_run(self):
        s = self._real('test("lower @tg:j1", () => { throw new Error("ran"); });\n')
        self.assertEqual((s["uncovered"], s["runner_exit"]), (["J1"], 0))

    def test_P6_max_failures_still_fails_the_journey(self):
        s = self._real('test("a @tg:J1", () => { throw new Error("red"); });\n'
                       'test("b @tg:J1", () => { throw new Error("red"); });\n', args=("-x",))
        self.assertEqual(s["credited"], {"J1": "fail"})
        self.assertEqual(verify.exit_code(s), verify.EXIT_TESTS_FAILED)

    def test_P7_test_fail_is_an_expected_failure(self):
        s = self._real('test.fail("xf @tg:J1", () => { throw new Error("known"); });\n')
        self.assertEqual(s["incomplete"], [("J1", "expected-fail")])

    def test_P4_an_inherited_json_output_file_is_ignored(self):
        theirs = os.path.join(self.tmp, "theirs.json")
        with unittest.mock.patch.dict(os.environ, {"PLAYWRIGHT_JSON_OUTPUT_FILE": theirs}):
            s = self._real('test("adds @tg:J1", () => {});\n')
        self.assertEqual(s["credited"], {"J1": "pass"})
        self.assertFalse(os.path.exists(theirs))


if __name__ == "__main__":
    unittest.main()
