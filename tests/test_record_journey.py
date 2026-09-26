"""J5 end to end: `python3 -m testgraph.record`, with nothing mocked.

`tests/test_record.py` is thorough, but it stubs `ledger.resolve_commit` on
every path that writes, so no test ever ran the command the way a user does —
against a real git repo whose commits git itself resolves. This one does: a
real repo with two commits, the CLI's own `main()` writing outcomes, and the
summary read back through the same CLI. The only thing it isolates is WHERE the
ledger lives (`TESTGRAPH_STATE_DIR`), which is configuration, not behavior.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from testgraph import ledger
from testgraph import record
from testgraph.results import covers


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, name):
    with open(os.path.join(repo, name), "w") as f:
        f.write(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


@covers("J5")
class RecordJourneyTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": os.path.join(root, "state")})
        env.start()
        self.addCleanup(env.stop)
        # `<root>/alpha/{.bare,main}` — the layout repo_name reads the project from.
        self.repo = os.path.join(root, "alpha", "main")
        os.makedirs(os.path.join(root, "alpha", ".bare"))
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        self.base = _commit(self.repo, "a")
        self.head = _commit(self.repo, "b")
        self.registry = os.path.join(root, "alpha.json")
        with open(self.registry, "w") as f:
            json.dump({"target": "alpha", "approved": True, "journeys": {
                "J1": {"name": "one", "entries": []},
                "J2": {"name": "two", "entries": []},
            }}, f)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = record.main(["--repo", self.repo, "--registry", self.registry, *argv])
        return code, out.getvalue()

    def test_recording_a_failure_git_resolves_and_the_summary_judges_it(self):
        # What the pre-push hook would have logged: this push named J2 only.
        ledger.append({"kind": ledger.SELECTION, "ts": 1, "repo": "alpha",
                       "commit": self.head, "base": self.base, "status": "OK",
                       "journey_ids": ["J2"]})

        # Both journeys known-good at the base, then both fail at head. No
        # --commit on the head rows: the CLI must resolve HEAD through git.
        for j in ("J1", "J2"):
            self.assertEqual(self.run_cli("--journey", j, "--outcome", "pass",
                                          "--commit", self.base)[0], 0)
        for j in ("J1", "J2"):
            code, out = self.run_cli("--journey", j, "--outcome", "fail")
            self.assertEqual(code, 0)
            self.assertIn(f"recorded {j} fail at {self.head[:9]} in alpha", out)

        code, out = self.run_cli("--summary", "--json")
        self.assertEqual(code, 0)
        summary = json.loads(out)["summary"]
        # J2 was named and broke: caught. J1 was not named and broke: the
        # silent under-selection the ledger exists to surface.
        self.assertEqual(summary["journeys"]["J2"]["caught"], 1)
        self.assertEqual(summary["journeys"]["J1"]["missed"], 1)
        self.assertEqual(summary["observed_recall"], 0.5)

        code, out = self.run_cli("--summary")
        self.assertEqual(code, 0)
        self.assertIn("testgraph ledger[alpha]", out)

    def test_a_rev_git_cannot_resolve_is_refused_not_stored(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("--journey", "J1", "--outcome", "fail",
                                   "--commit", "no-such-rev")
        self.assertEqual(code, 2)
        self.assertIn("cannot resolve", err.getvalue())
        self.assertEqual(ledger.read("alpha"), [])


if __name__ == "__main__":
    unittest.main()
