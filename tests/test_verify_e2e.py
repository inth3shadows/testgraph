"""`testgraph.verify` end to end, with REAL pytest and the tgtrace plugin (#114).

Every other verify test fakes the pytest run (`_runner=`), so tgtrace's hooks,
the JUnit join and `verify.run`'s trace handling were never exercised together
in CI — the `unittest-with-pytest` job installed pytest and then ran a suite
that never needed it. Skipped where pytest is not installed, so the stdlib-only
matrix still passes.

One target repo, one journey (J1 → `handler_a`), one committed diff that
selects J1. Each case swaps the declared test file and checks the exit code
the audit fixed for that shape.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)
from testgraph import verify  # noqa: E402
from test_core import _db_on_disk, _registry_file, build_fixture  # noqa: E402

HAVE_PYTEST = importlib.util.find_spec("pytest") is not None

# handler_a must sit on lines 10-20: that is where build_fixture's node says it is.
SVC = "".join(f"# line {i}\n" for i in range(1, 10)) + (
    "def handler_a():\n"
    "    return 1\n"
)

HEADER = (
    "import pytest\n"
    "from testgraph.results import covers\n"
    "from app.svc import handler_a\n\n\n"
)

CASES = {
    "pass": ("@covers('J1')\ndef test_j1():\n    assert handler_a() == 1\n",
             verify.EXIT_OK),
    "fail": ("@covers('J1')\ndef test_j1():\n    assert handler_a() == 2\n",
             verify.EXIT_TESTS_FAILED),
    "skip in the body": (
        "@covers('J1')\ndef test_j1():\n    handler_a()\n    pytest.skip('later')\n",
        verify.EXIT_INCOMPLETE),
    "xfail": (
        "@pytest.mark.xfail(reason='known')\n@covers('J1')\n"
        "def test_j1():\n    handler_a()\n    assert False\n",
        verify.EXIT_INCOMPLETE),
    "setup error": (
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('db down')\n\n\n"
        "@covers('J1')\ndef test_j1(broken):\n    assert handler_a() == 1\n",
        verify.EXIT_TESTS_FAILED),
    "parametrized id containing ::": (
        "@pytest.mark.parametrize('x', ['a::b'])\n@covers('J1')\n"
        "def test_j1(x):\n    assert handler_a() == 1\n",
        verify.EXIT_OK),
    "declared but never enters the journey": (
        "@covers('J1')\ndef test_j1():\n    assert True\n",
        verify.EXIT_INCOMPLETE),
}


@unittest.skipUnless(HAVE_PYTEST, "pytest is not installed")
class RealPytestVerifyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tg-e2e-")
        src = build_fixture()
        cls.db = _db_on_disk(cls.tmp, src)
        src.close()
        cls.registry = _registry_file(cls.tmp, {
            "J1": {"name": "one",
                   "entries": [{"name": "handler_a", "file": "app/svc.py"}]},
        })
        cls.repo = os.path.join(cls.tmp, "repo")
        os.makedirs(os.path.join(cls.repo, "app"))
        os.makedirs(os.path.join(cls.repo, "tests"))
        for rel, text in (("app/__init__.py", ""), ("app/svc.py", SVC),
                          # rootdir conftest: pytest puts the repo on sys.path
                          ("conftest.py", "")):
            with open(os.path.join(cls.repo, rel), "w") as fh:
                fh.write(text)

        def git(*args):
            subprocess.run(["git", "-C", cls.repo, *args], check=True,
                           capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "base")
        lines = SVC.splitlines(keepends=True)
        lines[10] = "    return 1  # touched\n"   # inside handler_a -> J1
        with open(os.path.join(cls.repo, "app", "svc.py"), "w") as fh:
            fh.writelines(lines)
        git("add", "-A")
        git("commit", "-qm", "touch handler_a")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _verify(self, body):
        # A fresh file per case, and no bytecode. Rewriting one test file within
        # the same second with a same-size body (`== 1` -> `== 2`) let Python
        # reuse the stale .pyc, which its size+mtime check calls fresh: the
        # "fail" case ran the "pass" code and exited 0.
        tests_dir = os.path.join(self.repo, "tests")
        shutil.rmtree(tests_dir, ignore_errors=True)
        os.makedirs(tests_dir)
        self._n = getattr(self, "_n", 0) + 1
        with open(os.path.join(tests_dir, f"test_case_{self._n}.py"), "w") as fh:
            fh.write(HEADER + body)
        # The pytest subprocess imports `testgraph.results` from this checkout.
        env = {"PYTHONPATH": os.pathsep.join(
            p for p in (ROOT_DIR, os.environ.get("PYTHONPATH")) if p),
            "PYTHONDONTWRITEBYTECODE": "1"}
        with unittest.mock.patch.dict(os.environ, env):
            return verify.run(self.repo, "HEAD~1", "HEAD",
                              registry_path=self.registry, db_path=self.db,
                              pytest_args=("tests", "-q", "-p", "no:cacheprovider"),
                              append=lambda row: True)

    def test_each_shape_gets_its_exit_code(self):
        for label, (body, want) in CASES.items():
            with self.subTest(label):
                summary, err = self._verify(body)
                self.assertIsNone(err, err)
                self.assertEqual(summary["journeys"], ["J1"])
                self.assertEqual(verify.exit_code(summary), want,
                                 (label, summary["credited"], summary["incomplete"],
                                  summary["uncovered"], summary["unvalidated"],
                                  summary["pytest_exit"]))

    def test_a_pass_is_validated_against_the_trace(self):
        summary, err = self._verify(CASES["pass"][0])
        self.assertIsNone(err, err)
        self.assertEqual(summary["credited"], {"J1": "pass"})
        self.assertEqual(summary["collected"], 1)
        self.assertTrue(summary["per_journey"]["J1"])


if __name__ == "__main__":
    unittest.main()
