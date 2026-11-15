"""The Vitest adapter (#122): a per-test V8 trace plus Vitest's JSON report,
joined into the same journey attribution the pytest adapter produces.

Hermetic: no Node. The runner is replaced by a function that writes what a
real run leaves in the work dir (the reporter's JSON and the setup file's
part files). The setup file itself (harness/plugin/tgtrace-vitest.ts) is
exercised against a real Vitest 4 suite outside CI; see the PR for that run.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph import pytest_adapter as pa  # noqa: E402
from testgraph import vitest_adapter as va  # noqa: E402
from test_core import _db_on_disk, _git_repo, _registry_file, build_fixture  # noqa: E402


def _report(repo, cases):
    """A Vitest JSON report: cases are (relfile, ancestors, title, status)."""
    files = {}
    for rel, ancestors, title, status in cases:
        files.setdefault(rel, []).append(
            {"ancestorTitles": list(ancestors), "title": title, "status": status})
    return {"testResults": [{"name": os.path.join(repo, rel), "assertionResults": a}
                            for rel, a in files.items()]}


class IdAndReportTests(unittest.TestCase):
    def test_the_id_carries_the_describe_chain(self):
        self.assertEqual(
            va.test_id("/r", "/r/src/a.test.ts", ["outer", "inner"], "does x"),
            "src/a.test.ts::outer > inner > does x")
        self.assertEqual(va.test_id("/r", "/r/a.test.ts", [], "top"), "a.test.ts::top")

    def test_statuses_map_to_run_results(self):
        report = _report("/r", [("a.test.ts", [], t, st) for t, st in (
            ("p", "passed"), ("f", "failed"), ("s", "skipped"), ("n", "pending"),
            ("t", "todo"), ("weird", "something-new"))])
        got = {v["test_id"].split("::")[1]: v["status"]
               for v in va.parse_report(report, "/r").values()}
        self.assertEqual(got, {"p": "pass", "f": "fail", "s": "skip", "n": "skip",
                               "t": "skip", "weird": "skip"})

    def test_report_keys_are_the_keys_attribute_joins_on(self):
        report = _report("/r", [("src/a.test.ts", ["d"], "x", "passed")])
        key, = va.parse_report(report, "/r")
        self.assertEqual(key, pa._join_key("src/a.test.ts::d > x"))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_node_modules_is_found_at_a_workspace_root(self):
        nm = os.path.join(self.tmp, "node_modules")
        os.makedirs(os.path.join(nm, "vitest"))
        with open(os.path.join(nm, "vitest", "package.json"), "w") as fh:
            fh.write("{}")
        pkg = os.path.join(self.tmp, "packages", "app")
        os.makedirs(pkg)
        self.assertEqual(va.find_node_modules(pkg), nm)

    def test_no_vitest_install_is_none(self):
        os.makedirs(os.path.join(self.tmp, "node_modules", "react"))
        self.assertIsNone(va.find_node_modules(self.tmp))

    def test_vitest_config_wins_over_vite_config(self):
        for name in ("vite.config.ts", "vitest.config.mts"):
            open(os.path.join(self.tmp, name), "w").close()
        self.assertEqual(va.find_config(self.tmp),
                         os.path.join(self.tmp, "vitest.config.mts"))
        self.assertIsNone(va.find_config(os.path.join(self.tmp, "..", "nope")))

    def test_the_generated_config_merges_into_the_repos_own(self):
        src = va.config_source("/r", "/r/vitest.config.ts", "/tmp/x/tgtrace-vitest.ts")
        self.assertIn('import base from "/r/vitest.config.ts"', src)
        self.assertIn('typeof base === "function"', src)
        self.assertIn("mergeConfig(resolved", src)
        self.assertIn('setupFiles: ["/tmp/x/tgtrace-vitest.ts"]', src)
        self.assertIn('root: "/r"', src)
        bare = va.config_source("/r", None, "/s.ts")
        self.assertNotIn("import base", bare)
        self.assertIn("const resolved = {}", bare)


class _VitestRunFixture:
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.db = _db_on_disk(tmp, build_fixture())
        self.registry = _registry_file(tmp, {
            "J1": {"name": "ok", "entries": [{"name": "handler_a", "file": "app/svc.py"}]},
        })
        self.repo, _ = _git_repo(tmp, {"app/svc.py": 22})
        self.repo = os.path.abspath(self.repo)
        self.work = os.path.join(tmp, "work")
        os.makedirs(self.work)

    def _part(self, tests, name="p1.json", text=None):
        os.makedirs(os.path.join(self.work, "parts"), exist_ok=True)
        with open(os.path.join(self.work, "parts", name), "w") as fh:
            fh.write(text if text is not None else json.dumps(
                {"root": self.repo, "backend": va.BACKEND, "file": "x", "tests": tests}))

    def _run(self, report=None, parts=(), stderr="", report_text=None, **kw):
        work = self.work

        def runner():
            for args in parts:
                self._part(*args) if isinstance(args, tuple) else self._part(args)
            if report_text is not None or report is not None:
                with open(os.path.join(work, "results.json"), "w") as fh:
                    fh.write(report_text if report_text is not None else json.dumps(report))
            return types.SimpleNamespace(returncode=1 if "failed" in str(report) else 0,
                                         stderr=stderr)

        return va.run(self.repo, registry_path=self.registry, db_path=self.db,
                      _runner=runner, _workdir=work, append=lambda r: True, **kw)


class RunTests(_VitestRunFixture, unittest.TestCase):
    TID = "src/a.test.ts::svc > calls handler"

    def test_a_test_that_ran_an_entry_covers_its_journey(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed"),
                                     ("src/a.test.ts", [], "internal", "passed")])
        summary, err = self._run(report, parts=[{
            self.TID: [["app/svc.py", "handler_a"]],
            "src/a.test.ts::internal": [["app/svc.py", "no_such_fn"]],
        }])
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {"J1": "pass"})
        self.assertEqual(summary["covering_tests"], {"J1": 1})
        self.assertEqual(summary["tests_covering_no_journey"], 1)
        self.assertEqual(summary["traced_without_verdict"], [])
        self.assertEqual(summary["verdicts_without_trace"], [])
        self.assertEqual(summary["runner"], "vitest")
        self.assertEqual(summary["rows_written"], 1)

    def test_a_failing_covering_test_fails_the_journey(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "failed")])
        summary, err = self._run(report, parts=[{self.TID: [["app/svc.py", "handler_a"]]}])
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {"J1": "fail"})
        self.assertEqual(summary["runner_exit"], 1)

    def test_parts_from_several_test_files_merge(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed"),
                                     ("src/b.test.ts", [], "also", "passed")])
        summary, err = self._run(report, parts=[
            ({self.TID: [["app/svc.py", "handler_a"]]}, "p1.json"),
            ({"src/b.test.ts::also": [["app/svc.py", "handler_a"]]}, "p2.json")])
        self.assertIsNone(err)
        self.assertEqual(summary["covering_tests"], {"J1": 2})

    def test_render_names_the_runner(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed")])
        summary, _ = self._run(report, parts=[{self.TID: [["app/svc.py", "handler_a"]]}])
        self.assertIn("testgraph vitest-adapter[", pa.render(summary))
        self.assertIn("(vitest exit 0, v8-precise-coverage)", pa.render(summary))

    def test_trace_out_gets_the_merged_trace(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed")])
        out = os.path.join(self.tmp, "trace.json")
        summary, err = self._run(report, parts=[{self.TID: [["app/svc.py", "handler_a"]]}],
                                 trace_out=out)
        self.assertIsNone(err)
        with open(out) as fh:
            trace = json.load(fh)
        self.assertEqual(trace["tests"], {self.TID: [["app/svc.py", "handler_a"]]})
        self.assertEqual(trace["backend"], va.BACKEND)


class RefusalTests(_VitestRunFixture, unittest.TestCase):
    """A run that cannot be read is refused, never attributed (#108, #154)."""

    def test_no_report_is_refused_with_stderr(self):
        summary, err = self._run(None, stderr="Error: Cannot find module 'vitest/config'")
        self.assertIsNone(summary)
        self.assertIn("wrote no JSON report", err)
        self.assertIn("vitest/config", err)

    def test_a_truncated_report_is_refused(self):
        summary, err = self._run(report_text='{"testResults": [')
        self.assertIn("truncated or not JSON", err)

    def test_a_report_that_is_not_an_object_is_refused(self):
        summary, err = self._run(report_text="[]")
        self.assertIn("not an object", err)

    def test_tests_with_no_trace_mean_the_setup_file_did_not_load(self):
        report = _report(self.repo, [("src/a.test.ts", [], "x", "passed")])
        summary, err = self._run(report, stderr="boom")
        self.assertIsNone(summary)
        self.assertIn("setup file did not load", err)

    def test_a_truncated_part_is_refused(self):
        report = _report(self.repo, [("src/a.test.ts", [], "x", "passed")])
        summary, err = self._run(report, parts=[({}, "p1.json", '{"tests": {')])
        self.assertIn("truncated or not JSON", err)

    def test_a_part_with_the_wrong_shape_is_refused(self):
        report = _report(self.repo, [("src/a.test.ts", [], "x", "passed")])
        summary, err = self._run(report, parts=[({"t": [1]}, "p1.json")])
        self.assertIn("not a tgtrace-vitest part", err)

    def test_an_empty_suite_is_not_a_refusal(self):
        summary, err = self._run({"testResults": []})
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {})

    def test_no_vitest_install_is_refused_before_running(self):
        summary, err = va.run(self.repo, registry_path=self.registry, db_path=self.db,
                              _workdir=self.work)
        self.assertIsNone(summary)
        self.assertIn("no node_modules with vitest", err)

    def test_a_bad_trace_out_is_refused_before_running(self):
        ran = []

        def runner():
            ran.append(1)
            return types.SimpleNamespace(returncode=0, stderr="")
        summary, err = va.run(self.repo, registry_path=self.registry, db_path=self.db,
                              _runner=runner, _workdir=self.work,
                              trace_out="/dev/stdout")
        self.assertIn("--trace-out", err)
        self.assertEqual(ran, [])

    def test_main_json_refusal_is_json(self):
        out = io.StringIO()
        with unittest.mock.patch.object(va, "run", return_value=(None, "broke")), \
                contextlib.redirect_stdout(out):
            rc = va.main(["--repo", "/x", "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue()), {"refused": True, "error": "broke"})


class PluginShipsTests(unittest.TestCase):
    def test_the_setup_file_sits_beside_tgtrace(self):
        plugins = pa.plugin_dir()
        self.assertTrue(os.path.isfile(os.path.join(plugins, va.PLUGIN_NAME)))

    def test_the_setup_file_imports_nothing_from_testgraph(self):
        """It runs in the TARGET repo's workers, which cannot import us."""
        with open(os.path.join(pa.plugin_dir(), va.PLUGIN_NAME)) as fh:
            imports = [l for l in fh if l.startswith("import ")]
        self.assertTrue(imports)
        for line in imports:
            self.assertRegex(line, r'from "(node:[a-z/]+|vitest)";')


if __name__ == "__main__":
    unittest.main()
