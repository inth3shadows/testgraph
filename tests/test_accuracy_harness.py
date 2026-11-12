"""harness/accuracy.py verdict (#97): a run that scored nothing must not PASS."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from harness import _worktree, accuracy  # noqa: E402


def _fail_add(*_a, **_k):
    raise _worktree.WorktreeError("WORKTREE-FAIL", "no such bare repo")


class AccuracyVerdict(unittest.TestCase):
    def _run(self, add_and_index, select=None):
        with mock.patch.object(accuracy._worktree, "remove"), \
             mock.patch.object(accuracy._worktree, "add_and_index", add_and_index), \
             mock.patch.object(accuracy.sel, "select", select or mock.DEFAULT), \
             mock.patch("builtins.print") as out:
            rc = accuracy.main()
        text = "\n".join(" ".join(map(str, c.args)) for c in out.call_args_list)
        return rc, text

    def test_every_worktree_failing_is_a_fail_not_a_pass(self):
        rc, text = self._run(_fail_add)
        self.assertEqual(rc, 1)
        self.assertIn("S1 VERDICT : FAIL", text)
        self.assertIn("commits scored: 0", text)

    def test_one_blocked_commit_fails_the_run(self):
        labels = accuracy.json.load(open(os.path.join(accuracy.HERE, "labels_honeyslate.json")))
        first = labels["commits"][0]["sha"]

        def select(wtdir, base, sha, db, reg, strict_registry):
            if sha == first:
                return {"status": "BLOCKED", "blocking": ["registry rot"]}
            oracle = next(c["oracle"] for c in labels["commits"] if c["sha"] == sha)
            return {"status": "OK", "journeys": [{"id": j} for j in oracle]}

        rc, text = self._run(lambda *a, **k: "db", select)
        self.assertEqual(rc, 1)
        self.assertIn("errored or blocked: 1", text)

    def test_perfect_recall_on_every_commit_passes(self):
        labels = accuracy.json.load(open(os.path.join(accuracy.HERE, "labels_honeyslate.json")))

        def select(wtdir, base, sha, db, reg, strict_registry):
            oracle = next(c["oracle"] for c in labels["commits"] if c["sha"] == sha)
            return {"status": "OK", "journeys": [{"id": j} for j in oracle]}

        rc, text = self._run(lambda *a, **k: "db", select)
        self.assertEqual(rc, 0, text)
        self.assertIn("S1 VERDICT : PASS", text)


if __name__ == "__main__":
    unittest.main()
