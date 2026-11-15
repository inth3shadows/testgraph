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
import subprocess
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
            ("p", "passed"), ("f", "failed"), ("s", "skipped"),
            ("t", "todo"), ("weird", "something-new"))])
        verdicts, err = va.parse_report(report, "/r")
        self.assertIsNone(err)
        got = {v["test_id"].split("::")[1]: v["status"] for v in verdicts.values()}
        self.assertEqual(got, {"p": "pass", "f": "fail", "s": "skip",
                               "t": "skip", "weird": "skip"})

    def test_report_keys_are_the_keys_attribute_joins_on(self):
        report = _report("/r", [("src/a.test.ts", ["d"], "x", "passed")])
        verdicts, err = va.parse_report(report, "/r")
        key, = verdicts
        self.assertEqual(key, pa._join_key("src/a.test.ts::d > x"))

    def test_an_empty_describe_name_is_kept_as_the_reporter_keeps_it(self):
        """V9: ancestorTitles [""] -> an id with an empty segment, which the
        setup file must build the same way."""
        report = _report("/r", [("a.test.ts", [""], "x", "passed")])
        verdicts, _ = va.parse_report(report, "/r")
        key, = verdicts
        self.assertEqual(verdicts[key]["test_id"], "a.test.ts:: > x")


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
        # V6: no forced root; cwd is the repo, and the repo's own `root` must win.
        self.assertNotIn("root:", src)
        # V12: a Promise default export is awaited like a function's result.
        self.assertIn("await (typeof base", src)
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

    def test_tests_with_no_trace_mean_the_setup_file_did_not_reach_them(self):
        report = _report(self.repo, [("src/a.test.ts", [], "x", "passed")])
        summary, err = self._run(report, stderr="boom")
        self.assertIsNone(summary)
        self.assertIn("did not load or did not reach the tests", err)
        self.assertIn("does not inherit setupFiles", err)

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


class Round1RefusalTests(_VitestRunFixture, unittest.TestCase):
    """Review round 1 (V1-V12): a broken or partial run is a refusal, never a
    verdict, never attribution."""
    A = "src/a.test.ts::svc > calls handler"
    B = "src/a.test.ts::other"

    def _two(self, st_b="passed"):
        return _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed"),
                                   ("src/a.test.ts", [], "other", st_b)])

    # V1 / V2
    def test_V1_a_passed_test_without_a_trace_is_refused(self):
        summary, err = self._run(self._two(), parts=[{self.A: [["app/svc.py", "handler_a"]]}])
        self.assertIsNone(summary)
        self.assertIn("1 of 2", err)
        self.assertIn("src/a.test.ts::other", err)

    def test_V1_V2_a_failed_test_without_a_trace_is_refused(self):
        """The repo's afterEach threw, our hook never ran: a verdict, no trace."""
        summary, err = self._run(self._two("failed"),
                                 parts=[{self.A: [["app/svc.py", "handler_a"]]}])
        self.assertIsNone(summary)
        self.assertIn("src/a.test.ts::other", err)

    def test_V1_skipped_and_todo_tests_need_no_trace(self):
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed"),
                                     ("src/a.test.ts", [], "s", "skipped"),
                                     ("src/a.test.ts", [], "t", "todo")])
        summary, err = self._run(report, parts=[{self.A: [["app/svc.py", "handler_a"]]}])
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {"J1": "pass"})

    def test_a_test_that_never_finished_is_refused(self):
        """A killed worker leaves its tests `pending`, which must not read as skip."""
        report = _report(self.repo, [("src/a.test.ts", ["svc"], "calls handler", "passed"),
                                     ("src/a.test.ts", [], "other", "pending")])
        summary, err = self._run(report, parts=[{self.A: [["app/svc.py", "handler_a"]]}])
        self.assertIsNone(summary)
        self.assertIn("never finished", err)
        self.assertIn("src/a.test.ts::other", err)

    # V3
    def test_V3_duplicate_ids_in_the_report_are_refused(self):
        report = _report(self.repo, [("src/a.test.ts", ["d"], "same", "passed"),
                                     ("src/a.test.ts", ["d"], "same", "failed")])
        summary, err = self._run(report, parts=[{"src/a.test.ts::d > same": []}])
        self.assertIsNone(summary)
        self.assertIn("share the id 'src/a.test.ts::d > same'", err)

    def test_V3_a_part_that_saw_a_duplicate_is_refused(self):
        report = _report(self.repo, [("src/a.test.ts", [], "x", "passed")])
        text = json.dumps({"tests": {"src/a.test.ts::x": []},
                           "duplicates": ["src/a.test.ts::x"]})
        summary, err = self._run(report, parts=[({}, "p1.json", text)])
        self.assertIsNone(summary)
        self.assertIn("share the id 'src/a.test.ts::x'", err)

    # V4
    def test_V4_a_file_that_failed_to_import_is_refused_by_name(self):
        report = {"testResults": [{"name": os.path.join(self.repo, "src/c.test.ts"),
                                   "status": "failed", "message": "Cannot find module './x'",
                                   "assertionResults": []}]}
        summary, err = self._run(report)
        self.assertIsNone(summary)
        self.assertIn("src/c.test.ts failed outside any test", err)
        self.assertIn("Cannot find module", err)

    def test_V4_a_throwing_beforeall_is_not_read_as_skips(self):
        """Vitest reports the file `failed` and its tests `skipped`."""
        report = _report(self.repo, [("src/b.test.ts", ["bb"], "t1", "skipped")])
        report["testResults"][0]["status"] = "failed"
        summary, err = self._run(report)
        self.assertIsNone(summary)
        self.assertIn("src/b.test.ts failed outside any test", err)

    def test_V4_a_file_with_a_failing_test_is_a_result(self):
        report = self._two("failed")
        report["testResults"][0]["status"] = "failed"
        summary, err = self._run(report, parts=[{self.A: [["app/svc.py", "handler_a"]],
                                                 self.B: []}])
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {"J1": "pass"})

    # V5
    def test_V5_a_symlinked_repo_is_compared_by_its_real_path(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.repo, link)
        report = _report(os.path.realpath(self.repo),
                         [("src/a.test.ts", ["svc"], "calls handler", "passed")])
        work = self.work

        def runner():
            self._part({self.A: [["app/svc.py", "handler_a"]]})
            with open(os.path.join(work, "results.json"), "w") as fh:
                json.dump(report, fh)
            return types.SimpleNamespace(returncode=0, stderr="")
        summary, err = va.run(link, registry_path=self.registry, db_path=self.db,
                              _runner=runner, _workdir=work, append=lambda r: True)
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {"J1": "pass"})
        self.assertEqual(summary["traced_without_verdict"], [])

    def _captured_command(self, repo):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return types.SimpleNamespace(returncode=0, stderr="")
        with unittest.mock.patch.object(va.subprocess, "run", fake_run):
            va.execute(repo, os.path.join(pa.plugin_dir(), va.PLUGIN_NAME), None, self.work)
        return calls[0]

    def test_V5_the_run_gets_the_real_path_as_cwd_and_trace_root(self):
        link = os.path.join(self.tmp, "link2")
        os.symlink(self.repo, link)
        _, kw = self._captured_command(link)
        self.assertEqual(kw["cwd"], os.path.realpath(self.repo))
        self.assertEqual(kw["env"]["TGTRACE_ROOT"], os.path.realpath(self.repo))

    # V8 / V10
    def test_V8_the_command_line_disables_coverage(self):
        cmd, _ = self._captured_command(self.repo)
        self.assertIn("--coverage.enabled=false", cmd)

    def test_V10_the_temp_node_modules_is_a_real_dir_of_links(self):
        """A directory symlink would send Vite's `.vite-temp` into the target."""
        src = os.path.join(self.tmp, "nm")
        os.makedirs(os.path.join(src, "vitest"))
        os.makedirs(os.path.join(src, ".vite-temp"))
        os.makedirs(os.path.join(src, ".bin"))
        dest = os.path.join(self.tmp, "t", "node_modules")
        va.link_node_modules(src, dest)
        self.assertFalse(os.path.islink(dest))
        self.assertTrue(os.path.islink(os.path.join(dest, "vitest")))
        self.assertTrue(os.path.islink(os.path.join(dest, ".bin")))
        self.assertFalse(os.path.lexists(os.path.join(dest, ".vite-temp")))

    # V7
    def test_V7_an_all_skipped_run_is_not_a_refusal(self):
        report = _report(self.repo, [("src/a.test.ts", [], "s", "skipped"),
                                     ("src/a.test.ts", [], "t", "todo")])
        summary, err = self._run(report)
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], {})

    def test_V7_a_filter_that_matches_nothing_is_not_a_refusal(self):
        summary, err = self._run({"testResults": [{
            "name": os.path.join(self.repo, "src/a.test.ts"), "status": "passed",
            "assertionResults": []}]})
        self.assertIsNone(err)

    def test_V7_a_projects_config_is_refused_up_front(self):
        summary, err = self._run(None, stderr=f"Error: {va.PROJECTS_MARKER}\n   at ...")
        self.assertIsNone(summary)
        self.assertIn("test.projects", err)
        self.assertIn("does not support yet", err)

    def test_V7_a_workspace_file_is_refused_before_running(self):
        open(os.path.join(self.repo, "vitest.workspace.ts"), "w").close()
        ran = []

        def runner():
            ran.append(1)
            return types.SimpleNamespace(returncode=0, stderr="")
        summary, err = va.run(self.repo, registry_path=self.registry, db_path=self.db,
                              _runner=runner, _workdir=self.work)
        self.assertIsNone(summary)
        self.assertIn("workspace", err)
        self.assertEqual(ran, [])

    def test_V7_the_generated_config_refuses_a_projects_config(self):
        src = va.config_source("/r", "/r/vitest.config.ts", "/s.ts")
        self.assertIn("resolved?.test?.projects", src)
        self.assertIn(va.PROJECTS_MARKER, src)

    # V11
    def test_V11_a_malformed_report_is_a_refusal_not_a_crash(self):
        bad = [
            {},
            {"testResults": None},
            {"testResults": [None]},
            {"testResults": [{"assertionResults": []}]},
            {"testResults": [{"name": "/x/a.ts", "assertionResults": None}]},
            {"testResults": [{"name": "/x/a.ts", "assertionResults": [None]}]},
            {"testResults": [{"name": "/x/a.ts", "assertionResults": [
                {"title": None, "ancestorTitles": [], "status": "passed"}]}]},
            {"testResults": [{"name": "/x/a.ts", "assertionResults": [
                {"title": "t", "ancestorTitles": None, "status": "passed"}]}]},
            {"testResults": [{"name": "/x/a.ts", "assertionResults": [
                {"title": "t", "ancestorTitles": [1], "status": "passed"}]}]},
        ]
        for report in bad:
            with self.subTest(report=report):
                summary, err = self._run(report)
                self.assertIsNone(summary)
                self.assertIn("unexpected shape", err)


VITEST_NODE_MODULES = os.environ.get(
    "TG_VITEST_NODE_MODULES",
    "/home/ericm/personal_projects/signedintake/main/node_modules")
HAVE_VITEST = bool(shutil.which("node")) and os.path.isfile(
    os.path.join(VITEST_NODE_MODULES, "vitest", "package.json"))

LIB = "export function handlerA() { return 1; }\n"
CONFIG = ('import { defineConfig } from "vitest/config";\n'
          'export default defineConfig({ test: { include: ["src/**/*.test.ts"] } });\n')


@unittest.skipUnless(HAVE_VITEST, "needs node and a vitest install "
                     "(TG_VITEST_NODE_MODULES, default: signedintake's node_modules)")
class RealVitestTests(unittest.TestCase):
    """The setup file and the adapter against a real Vitest run (#122, review
    round 1). Drives `execute`, the traced run `run()` is built from; the
    codegraph join is covered by the hermetic tests."""

    def _project(self, files, config=CONFIG):
        d = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # A real node_modules of symlinks to each entry, not one symlink to the
        # directory: Vitest/Vite write caches into node_modules, which must
        # land here and never in the shared install.
        nm = os.path.join(d, "node_modules")
        os.mkdir(nm)
        for entry in os.listdir(VITEST_NODE_MODULES):
            if entry not in (".vite", ".vite-temp", ".cache"):
                os.symlink(os.path.join(VITEST_NODE_MODULES, entry), os.path.join(nm, entry))
        os.makedirs(os.path.join(d, "src"))
        with open(os.path.join(d, "src", "lib.ts"), "w") as fh:
            fh.write(LIB)
        if config:
            with open(os.path.join(d, "vitest.config.ts"), "w") as fh:
                fh.write(config)
        for name, text in files.items():
            with open(os.path.join(d, "src", name), "w") as fh:
                fh.write(text)
        return d

    def _exec(self, d, repo=None):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        plugin = os.path.join(pa.plugin_dir(), va.PLUGIN_NAME)
        return va.execute(repo or d, plugin, os.path.join(d, "node_modules"), work)

    @staticmethod
    def _keys(trace, verdicts):
        return ({pa._join_key(t) for t in trace["tests"]}, set(verdicts))

    def test_V2_a_throwing_repo_afterEach_does_not_cost_the_trace(self):
        d = self._project({"a.test.ts": (
            'import { it, expect, afterEach } from "vitest";\n'
            'import { handlerA } from "./lib";\n'
            'afterEach(() => { throw new Error("repo afterEach"); });\n'
            'it("passes", () => { handlerA(); });\n'
            'it("fails", () => { handlerA(); expect(1).toBe(2); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)
        self.assertEqual({v["status"] for v in verdicts.values()}, {"fail"})
        self.assertEqual(trace["tests"], {
            "src/a.test.ts::passes": [["src/lib.ts", "handlerA"]],
            "src/a.test.ts::fails": [["src/lib.ts", "handlerA"]]})

    def test_V1_a_part_that_never_landed_is_a_refusal(self):
        d = self._project({
            "a.test.ts": 'import { it } from "vitest";\nit("x", () => {});\n',
            "b.test.ts": 'import { it } from "vitest";\nit("y", () => {});\n'})
        real_run = subprocess.run

        def lose_a_part(cmd, **kw):
            proc = real_run(cmd, **kw)
            parts = kw["env"]["TGTRACE_OUT_DIR"]
            os.remove(os.path.join(parts, sorted(os.listdir(parts))[0]))
            return proc
        with unittest.mock.patch.object(va.subprocess, "run", lose_a_part):
            proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(trace)
        self.assertIn("1 of 2 passed/failed tests have no trace", err)

    def test_a_killed_worker_is_a_refusal_not_a_skip(self):
        d = self._project({"k.test.ts": (
            'import { it } from "vitest";\n'
            'it("dies", () => { process.kill(process.pid, "SIGKILL"); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(trace)
        self.assertIsNotNone(err)

    def test_V3_duplicate_titles_are_a_refusal(self):
        d = self._project({"a.test.ts": (
            'import { it, expect, describe } from "vitest";\n'
            'describe("dup", () => { it("same", () => {});'
            ' it("same", () => { expect(1).toBe(2); }); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(trace)
        self.assertIn("share the id 'src/a.test.ts::dup > same'", err)

    def test_V3_the_setup_file_flags_a_duplicate_in_its_own_part(self):
        d = self._project({"a.test.ts": (
            'import { it, describe } from "vitest";\n'
            'describe("dup", () => { it("same", () => {}); it("same", () => {}); });\n')})
        seen = []
        real_merge = va.merge_parts

        def spy(out_dir):
            for name in os.listdir(out_dir):
                with open(os.path.join(out_dir, name)) as fh:
                    seen.append(json.load(fh))
            return real_merge(out_dir)
        # parse_report would refuse the duplicate first; bypass it to see the part.
        with unittest.mock.patch.object(va, "merge_parts", spy), \
                unittest.mock.patch.object(va, "parse_report", lambda r, root: ({}, None)):
            self._exec(d)
        self.assertEqual([p["duplicates"] for p in seen], [["src/a.test.ts::dup > same"]])

    def test_V3_a_retried_test_is_not_a_duplicate(self):
        d = self._project({"a.test.ts": (
            'import { it, expect } from "vitest";\n'
            'let n = 0;\n'
            'it("flaky", { retry: 2 }, () => { n++; expect(n).toBe(2); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)
        self.assertEqual({v["status"] for v in verdicts.values()}, {"pass"})

    def test_V9_an_empty_describe_name_joins(self):
        d = self._project({"a.test.ts": (
            'import { it, describe } from "vitest";\n'
            'import { handlerA } from "./lib";\n'
            'describe("", () => { it("x", () => { handlerA(); }); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)
        traced, judged = self._keys(trace, verdicts)
        self.assertEqual(traced, judged)
        self.assertEqual(list(trace["tests"]), ["src/a.test.ts:: > x"])

    def test_V5_a_repo_spelled_through_a_symlink_still_traces(self):
        d = self._project({"a.test.ts": (
            'import { it } from "vitest";\n'
            'import { handlerA } from "./lib";\n'
            'it("x", () => { handlerA(); });\n')})
        link = os.path.join(tempfile.mkdtemp(), "link")
        self.addCleanup(shutil.rmtree, os.path.dirname(link), ignore_errors=True)
        os.symlink(d, link)
        proc, trace, verdicts, err = self._exec(d, repo=link)
        self.assertIsNone(err)
        self.assertEqual(trace["tests"], {"src/a.test.ts::x": [["src/lib.ts", "handlerA"]]})
        traced, judged = self._keys(trace, verdicts)
        self.assertEqual(traced, judged)

    def test_V4_import_failures_and_throwing_beforeAll_are_refusals(self):
        d = self._project({"c.test.ts": 'import "./nonexistent";\nimport { it } from "vitest";\nit("n", () => {});\n'})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIn("src/c.test.ts failed outside any test", err)
        d = self._project({"b.test.ts": (
            'import { it, describe, beforeAll } from "vitest";\n'
            'describe("bb", () => { beforeAll(() => { throw new Error("boom"); });'
            ' it("t1", () => {}); });\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIn("src/b.test.ts failed outside any test", err)

    def test_V7_an_all_skipped_run_is_an_empty_success(self):
        d = self._project({"d.test.ts": (
            'import { it } from "vitest";\nit.skip("s", () => {}); it.todo("t");\n')})
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)
        self.assertEqual({v["status"] for v in verdicts.values()}, {"skip"})

    def test_V7_a_projects_config_is_refused_with_its_cause(self):
        d = self._project({"a.test.ts": 'import { it } from "vitest";\nit("x", () => {});\n'},
                          config='import { defineConfig } from "vitest/config";\n'
                                 'export default defineConfig({ test: { projects: '
                                 '[{ test: { name: "u", include: ["src/**/*.test.ts"] } }] } });\n')
        proc, trace, verdicts, err = self._exec(d)
        self.assertIn("test.projects", err)

    def test_V12_a_promise_default_export_config_is_awaited(self):
        d = self._project({"a.test.ts": 'import { it } from "vitest";\nit("x", () => {});\n'},
                          config='export default (async () => ({ test: '
                                 '{ include: ["src/**/*.test.ts"] } }))();\n')
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)
        self.assertEqual(list(trace["tests"]), ["src/a.test.ts::x"])

    def test_V10_no_bundled_config_lands_in_the_targets_node_modules(self):
        d = self._project({"a.test.ts": 'import { it } from "vitest";\nit("x", () => {});\n'})
        self._exec(d)
        self.assertFalse(os.path.exists(os.path.join(d, "node_modules", ".vite-temp")))

    def test_a_config_using_dirname_loads(self):
        """`--configLoader runner` would break this; the default bundler defines it."""
        d = self._project({"a.test.ts": 'import { it } from "vitest";\nit("x", () => {});\n'},
                          config='import { defineConfig } from "vitest/config";\n'
                                 'export default defineConfig({ resolve: { alias: '
                                 '{ "@": __dirname + "/src" } }, test: { include: '
                                 '["src/**/*.test.ts"] } });\n')
        proc, trace, verdicts, err = self._exec(d)
        self.assertIsNone(err)


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
