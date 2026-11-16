"""`verify --runner vitest` (#122): run only the selected journeys' Vitest
tests, and validate each declaration against its own V8 trace.

A Vitest test declares its journey with `@tg:J1` in its title or in a describe
title (the same string Playwright's tag uses). Selection is Vitest's `-t`,
which matches the space-joined full name. Validation is `verify.validate`,
unchanged: a tagged test whose trace never reaches the journey's entry symbols
is `unvalidated`, never a pass.

The hermetic classes fake the Vitest run (report + part files). The real class
runs actual Vitest and skips where Node or a Vitest install is missing.
"""
import io
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph import verify  # noqa: E402
from testgraph import vitest_adapter as va  # noqa: E402
from test_core import _db_on_disk, _git_repo, _registry_file, build_fixture  # noqa: E402
from test_vitest_adapter import HAVE_VITEST, VITEST_NODE_MODULES, _report  # noqa: E402

JOURNEYS = {
    "J1": {"name": "one", "entries": [{"name": "handler_a", "file": "app/svc.py"}]},
    "J10": {"name": "ten", "entries": [{"name": "leaf", "file": "app/leaf.py"}]},
}


def _verdicts(cases):
    verdicts, err = va.parse_report(_report("/r", cases), "/r")
    assert err is None, err
    return verdicts


class TagTests(unittest.TestCase):
    """The `-t` pattern and the declaration parser must agree on what a tag is."""

    def test_the_pattern_matches_whole_tags_only(self):
        # Vitest compiles `-t` with `new RegExp`; this syntax is the same in Python.
        pat = re.compile(va.name_pattern(["J1"]))
        for name in ("src/a.test.ts pays @tg:J1",
                     "src/a.test.ts flow @tg:J1 pays",
                     "src/a.test.ts @tg:J1"):
            self.assertTrue(pat.search(name), name)
        for name in ("src/a.test.ts flow @tg:J10",
                     "src/a.test.ts flow x@tg:J1",
                     "src/a.test.ts flow @tg:J1x"):
            self.assertFalse(pat.search(name), name)

    def test_the_pattern_is_a_union_of_the_journeys(self):
        pat = re.compile(va.name_pattern(["J1", "J10"]))
        self.assertTrue(pat.search("f a @tg:J1"))
        self.assertTrue(pat.search("f a @tg:J10"))
        self.assertFalse(pat.search("f a @tg:J2"))

    def test_declared_reads_test_and_describe_titles(self):
        v = _verdicts([
            ("src/a.test.ts", [], "pays @tg:J1", "passed"),
            ("src/a.test.ts", ["checkout @tg:J1"], "pays", "passed"),
            ("src/a.test.ts", [], "both @tg:J1 @tg:J10", "passed"),
            ("src/a.test.ts", [], "only ten @tg:J10", "skipped"),
            ("src/a.test.ts", [], "plain", "skipped"),
            ("src/a.test.ts", [], "typo @tg:J1x", "skipped"),
        ])
        self.assertEqual(va.declared(v, ["J1", "J10"], ["J1"]), {
            "src/a.test.ts::pays @tg:J1": ["J1"],
            "src/a.test.ts::checkout @tg:J1 > pays": ["J1"],
            "src/a.test.ts::both @tg:J1 @tg:J10": ["J1", "J10"],
        })

    def test_declared_ignores_tags_the_registry_does_not_define(self):
        v = _verdicts([("src/a.test.ts", [], "x @tg:J1 @tg:J99", "passed")])
        self.assertEqual(va.declared(v, ["J1"], ["J1"]),
                         {"src/a.test.ts::x @tg:J1 @tg:J99": ["J1"]})


class _VitestVerifyFixture:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = _db_on_disk(self.tmp, build_fixture())
        self.registry = _registry_file(self.tmp, JOURNEYS)
        self.repo, run = _git_repo(self.tmp, {"app/svc.py": 22})
        self.repo = os.path.realpath(self.repo)
        full = os.path.join(self.repo, "app", "svc.py")
        with open(full) as fh:
            lines = fh.readlines()
        lines[11] = "changed = 1\n"  # inside handler_a -> J1 only
        with open(full, "w") as fh:
            fh.writelines(lines)
        run("git", "add", "-A")
        run("git", "commit", "-qm", "touch")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

    def _run(self, cases, tests, returncode=None, write_report=True):
        """cases: (ancestors, title, status); tests: {test_id: [[file, name]]}."""
        work, repo = self.work, self.repo

        def runner():
            os.makedirs(os.path.join(work, "parts"), exist_ok=True)
            with open(os.path.join(work, "parts", "p.json"), "w") as fh:
                json.dump({"root": repo, "backend": va.BACKEND, "file": "x",
                           "tests": tests}, fh)
            if write_report:
                with open(os.path.join(work, "results.json"), "w") as fh:
                    json.dump(_report(repo, [("src/a.test.ts", *c) for c in cases]), fh)
            failed = any(c[-1] == "failed" for c in cases)
            return types.SimpleNamespace(
                returncode=returncode if returncode is not None else int(failed),
                stderr="")

        return verify.run(self.repo, "HEAD~1", "HEAD", registry_path=self.registry,
                          db_path=self.db, runner="vitest", _runner=runner,
                          _workdir=work, append=lambda r: True)


SVC = [["app/svc.py", "handler_a"]]
LEAF = [["app/leaf.py", "leaf"]]


class VitestVerifyTests(_VitestVerifyFixture, unittest.TestCase):

    def test_a_tagged_test_that_reaches_the_entry_and_passes_is_credited(self):
        summary, err = self._run([((), "pays @tg:J1", "passed")],
                                 {"src/a.test.ts::pays @tg:J1": SVC})
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], ["J1"])
        self.assertEqual(summary["credited"], {"J1": "pass"})
        self.assertEqual(summary["runner"], "vitest")
        self.assertEqual(summary["marker_expression"], va.name_pattern(["J1"]))
        self.assertEqual(summary["collected"], 1)
        self.assertEqual(summary["rows_written"], 1)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_OK)

    def test_a_tag_on_the_describe_counts(self):
        summary, err = self._run([(("checkout @tg:J1",), "pays", "passed")],
                                 {"src/a.test.ts::checkout @tg:J1 > pays": SVC})
        self.assertIsNone(err)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_OK)

    def test_a_failing_tagged_test_fails_the_journey(self):
        summary, err = self._run([((), "pays @tg:J1", "failed")],
                                 {"src/a.test.ts::pays @tg:J1": SVC})
        self.assertIsNone(err)
        self.assertEqual(summary["credited"], {"J1": "fail"})
        self.assertEqual(verify.exit_code(summary), verify.EXIT_TESTS_FAILED)

    def test_a_tag_the_trace_does_not_support_is_unvalidated_not_a_pass(self):
        summary, err = self._run([((), "claims @tg:J1", "passed")],
                                 {"src/a.test.ts::claims @tg:J1": LEAF})
        self.assertIsNone(err)
        self.assertEqual(summary["unvalidated"], [("src/a.test.ts::claims @tg:J1", "J1")])
        self.assertEqual(summary["credited"], {})
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)

    def test_no_tagged_test_is_uncovered(self):
        summary, err = self._run([((), "plain", "skipped")], {})
        self.assertIsNone(err)
        self.assertEqual(summary["uncovered"], ["J1"])
        self.assertEqual(summary["collected"], 0)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)

    def test_a_J10_tag_does_not_cover_J1(self):
        summary, err = self._run([((), "ten @tg:J10", "skipped")], {})
        self.assertIsNone(err)
        self.assertEqual(summary["uncovered"], ["J1"])

    def test_a_nonzero_vitest_exit_downgrades_a_pass(self):
        summary, err = self._run([((), "pays @tg:J1", "passed")],
                                 {"src/a.test.ts::pays @tg:J1": SVC}, returncode=1)
        self.assertIsNone(err)
        self.assertEqual(summary["runner_exit"], 1)
        self.assertEqual(verify.exit_code(summary), verify.EXIT_INCOMPLETE)

    def test_a_broken_run_is_a_refusal(self):
        summary, err = self._run([((), "pays @tg:J1", "passed")], {}, write_report=False)
        self.assertIsNone(summary)
        self.assertIn("wrote no JSON report", err)

    def test_the_run_selects_with_dash_t(self):
        calls = []
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            # `va.subprocess` is the shared module: let git through.
            if not str(cmd[0]).endswith("vitest"):
                return real_run(cmd, **kw)
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stderr="")
        nm = os.path.join(self.tmp, "nm")
        os.makedirs(nm)
        with unittest.mock.patch.object(va.subprocess, "run", fake_run), \
                unittest.mock.patch.object(va, "find_node_modules", lambda repo: nm):
            verify.run(self.repo, "HEAD~1", "HEAD", registry_path=self.registry,
                       db_path=self.db, runner="vitest", _workdir=self.work,
                       append=lambda r: True)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("-t") + 1], va.name_pattern(["J1"]))

    def test_no_vitest_install_is_a_refusal(self):
        with unittest.mock.patch.object(va, "find_node_modules", lambda repo: None):
            summary, err = verify.run(self.repo, "HEAD~1", "HEAD",
                                      registry_path=self.registry, db_path=self.db,
                                      runner="vitest", append=lambda r: True)
        self.assertIsNone(summary)
        self.assertIn("no node_modules with vitest", err)

    def test_render_names_the_runner_and_the_tag(self):
        summary, _ = self._run([((), "plain", "skipped")], {})
        text = verify.render(summary)
        self.assertIn(f"vitest -t '{va.name_pattern(['J1'])}'", text)
        self.assertIn("@tg:", text)
        self.assertNotIn("covers(", text)

    def test_pytest_render_is_unchanged(self):
        summary = verify._summary(self.repo, "HEAD", {"base": "a", "head": "b"},
                                  journeys=["J1"], marker_expression="tg_J1",
                                  uncovered=["J1"], pytest_exit=5)
        text = verify.render(summary)
        self.assertIn("pytest -m 'tg_J1'", text)
        self.assertIn("covers(...)", text)

    def test_the_cli_takes_runner_vitest(self):
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return None, "stop here"
        out = io.StringIO()
        with unittest.mock.patch.object(verify, "run", fake_run), \
                contextlib.redirect_stdout(out):
            code = verify.main(["--repo", self.repo, "--base", "HEAD~1",
                                "--runner", "vitest", "--", "--bail=1"])
        self.assertEqual(code, verify.EXIT_REFUSED)
        self.assertEqual(seen["runner"], "vitest")
        self.assertEqual(seen["pytest_args"], ("--bail=1",))


LIB = "export function handlerA() {\n  return 1;\n}\n"
CONFIG = ('import { defineConfig } from "vitest/config";\n'
          'export default defineConfig({ test: { include: ["src/**/*.test.ts"] } });\n')
HEADER = ('import { it, describe, expect } from "vitest";\n'
          'import { handlerA } from "./lib";\n')


@unittest.skipUnless(HAVE_VITEST, "needs node and a vitest install "
                     "(TG_VITEST_NODE_MODULES, default: signedintake's node_modules)")
class RealVitestVerifyTests(unittest.TestCase):
    """verify end to end with real Vitest: one journey (J1 -> handlerA in
    src/lib.ts), one commit touching it, a test file per case."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix="tg-vv-"))
        src = build_fixture()
        src.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                    ("function:handlerA", "function", "handlerA", "handlerA",
                     "src/lib.ts", 1, 3))
        src.commit()  # backup() waits forever on an open write transaction
        cls.db = _db_on_disk(cls.tmp, src)
        src.close()
        cls.registry = _registry_file(cls.tmp, {
            "J1": {"name": "one", "entries": [{"name": "handlerA", "file": "src/lib.ts"}]},
            "J10": {"name": "ten", "entries": [{"name": "leaf", "file": "app/leaf.py"}]},
        })
        cls.repo = os.path.join(cls.tmp, "repo")
        os.makedirs(os.path.join(cls.repo, "src"))
        nm = os.path.join(cls.repo, "node_modules")
        os.mkdir(nm)
        for entry in os.listdir(VITEST_NODE_MODULES):
            if entry not in (".vite", ".vite-temp", ".cache"):
                os.symlink(os.path.join(VITEST_NODE_MODULES, entry), os.path.join(nm, entry))
        for rel, text in (("src/lib.ts", LIB), ("vitest.config.ts", CONFIG),
                          (".gitignore", "node_modules/\n")):
            with open(os.path.join(cls.repo, rel), "w") as fh:
                fh.write(text)

        def git(*args):
            subprocess.run(["git", "-C", cls.repo, *args], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "base")
        with open(os.path.join(cls.repo, "src", "lib.ts"), "w") as fh:
            fh.write(LIB.replace("return 1;", "return 1; // touched"))
        git("add", "-A")
        git("commit", "-qm", "touch handlerA")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _verify(self, body):
        for name in os.listdir(os.path.join(self.repo, "src")):
            if name.endswith(".test.ts"):
                os.remove(os.path.join(self.repo, "src", name))
        self._n = getattr(self, "_n", 0) + 1
        with open(os.path.join(self.repo, "src", f"case{self._n}.test.ts"), "w") as fh:
            fh.write(HEADER + body)
        summary, err = verify.run(self.repo, "HEAD~1", "HEAD",
                                  registry_path=self.registry, db_path=self.db,
                                  runner="vitest", append=lambda r: True)
        self.assertIsNone(err)
        self.assertEqual(summary["journeys"], ["J1"])
        return summary

    def test_only_the_selected_journeys_tests_run(self):
        # The untagged test would fail if it ran.
        s = self._verify('it("pays @tg:J1", () => { expect(handlerA()).toBe(1); });\n'
                         'it("other", () => { expect(1).toBe(2); });\n')
        self.assertEqual(s["credited"], {"J1": "pass"})
        self.assertEqual(s["collected"], 1)
        self.assertEqual(verify.exit_code(s), verify.EXIT_OK)

    def test_a_describe_tag_selects_its_tests(self):
        s = self._verify('describe("flow @tg:J1", () => {\n'
                         '  it("pays", () => { expect(handlerA()).toBe(1); });\n});\n')
        self.assertEqual(verify.exit_code(s), verify.EXIT_OK)

    def test_a_failing_tagged_test_exits_1(self):
        s = self._verify('it("pays @tg:J1", () => { expect(handlerA()).toBe(2); });\n')
        self.assertEqual(verify.exit_code(s), verify.EXIT_TESTS_FAILED)

    def test_a_tag_that_never_reaches_the_entry_is_unvalidated(self):
        s = self._verify('it("claims @tg:J1", () => { expect(1).toBe(1); });\n')
        self.assertEqual(len(s["unvalidated"]), 1)
        self.assertEqual(verify.exit_code(s), verify.EXIT_INCOMPLETE)

    def test_a_J10_tag_is_not_run_for_J1(self):
        s = self._verify('it("ten @tg:J10", () => { expect(1).toBe(2); });\n')
        self.assertEqual(s["uncovered"], ["J1"])
        self.assertEqual(verify.exit_code(s), verify.EXIT_INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
