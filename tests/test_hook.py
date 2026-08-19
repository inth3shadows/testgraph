"""The pre-push consumer (issue #49).

Two properties carry the whole feature and neither is about selection quality:
the hook must never fail a push, and it must record that it ran. Everything else
here is about not re-teaching the reader to ignore it.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from testgraph import hook
from testgraph import registry as reg


def _worktree_named(project):
    return _bare_worktree(tempfile.mkdtemp(), project, "main")


def _bare_worktree(root, project, leaf):
    """`<root>/<project>/{.bare,<leaf>}` — the layout repo-init produces."""
    path = os.path.join(root, project, leaf)
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(root, project, ".bare"), exist_ok=True)
    return path


class RepoNameTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_sees_through_the_bare_worktree_layout(self):
        for leaf in ("main", "claude-20260730-220007", "codex-1"):
            path = _bare_worktree(self.root, "signedintake", leaf)
            self.assertEqual(reg.repo_name(path), "signedintake", path)

    def test_a_plain_checkout_keeps_its_own_name(self):
        path = os.path.join(self.root, "honeyslate")
        os.makedirs(path, exist_ok=True)
        self.assertEqual(reg.repo_name(path), "honeyslate")

    def test_a_plain_repo_whose_name_starts_with_claude_is_not_promoted(self):
        # Decided on the `.bare` marker, not on the leaf's name. Matching
        # `claude-*` by name read ~/personal_projects/claude-code — an ordinary
        # checkout — as a worktree and answered `personal_projects`, so it could
        # never resolve its own registry and could collide with another project's.
        path = os.path.join(self.root, "claude-code")
        os.makedirs(path, exist_ok=True)
        self.assertEqual(reg.repo_name(path), "claude-code")

    def test_root_has_no_parent_to_promote(self):
        self.assertEqual(reg.repo_name("/"), "")


class ResolveForRepoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self._write("alpha.json", {"target": "alpha", "journeys": {}})
        # Filename and target deliberately disagree: the target is the claim.
        self._write("zzz.json", {"target": "beta", "journeys": {}})

    def _write(self, name, data):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(data, f)

    def test_matches_on_target_not_filename(self):
        repo = _bare_worktree(self.root, "beta", "main")
        self.assertTrue(reg.resolve_for_repo(repo, self.dir).endswith("zzz.json"))

    def test_returns_none_rather_than_guessing(self):
        # The defect this replaces: an unmatched repo silently loaded
        # honeyslate's registry and then blamed the index for disagreeing.
        repo = _bare_worktree(self.root, "gamma", "main")
        self.assertIsNone(reg.resolve_for_repo(repo, self.dir))

    def test_survives_an_unparseable_registry(self):
        with open(os.path.join(self.dir, "broken.json"), "w") as f:
            f.write("{not json")
        repo = _bare_worktree(self.root, "alpha", "main")
        self.assertTrue(reg.resolve_for_repo(repo, self.dir).endswith("alpha.json"))


SIGNEDINTAKE = _worktree_named("signedintake")
ALPHA = _worktree_named("alpha")
GAMMA = _worktree_named("gamma")


def _result(n_journeys, **over):
    base = {
        "status": "OK",
        "base": "a" * 40,
        "head": "b" * 40,
        "warnings": [],
        "entries_unchecked": [
            {"journey": f"J{i}", "entry": "x", "file": "p.tsx"} for i in range(14)
        ],
        "journeys": [
            {
                "id": f"J{i}",
                "name": f"journey {i}",
                "rank": 100 - i,
                "confidence": 1.0,
                "entries_hit": 1,
                "verify_manually": False,
            }
            for i in range(n_journeys)
        ],
    }
    base.update(over)
    return base


class RenderTest(unittest.TestCase):
    def test_drops_the_unchecked_entry_notes(self):
        # 14 identical NOTE lines on every signedintake push is how a reader
        # learns to skip the block — and the answer with it. They are also
        # permanent: no re-index can clear them.
        text = hook.render(_result(2), SIGNEDINTAKE)
        self.assertNotIn("NOTE", text)
        self.assertNotIn("p.tsx", text)

    def test_caps_the_journey_list_and_says_it_capped(self):
        text = hook.render(_result(12), SIGNEDINTAKE)
        self.assertIn("12 journey(s)", text)
        listed = [ln for ln in text.splitlines() if ln.startswith("  [")]
        self.assertEqual(len(listed), hook.MAX_JOURNEYS)
        self.assertIn(f"… {12 - hook.MAX_JOURNEYS} more", text)

    def test_keeps_the_signals_that_mean_the_answer_may_be_understated(self):
        text = hook.render(
            _result(1, recall_degraded=True, warnings=["registry not approved"]),
            SIGNEDINTAKE,
        )
        self.assertIn("RECALL DEGRADED", text)
        self.assertIn("registry not approved", text)

    def test_closure_confined_gets_its_own_line_like_recall_degraded(self):
        text = hook.render(_result(1, closure_confined=["app/leaf.py"]), SIGNEDINTAKE)
        self.assertIn("app/leaf.py", text)
        self.assertIn("did not leave the file", text)

    def test_closure_confined_line_is_capped_like_journeys_and_warnings(self):
        confined = [f"app/f{i}.py" for i in range(40)]
        text = hook.render(_result(1, closure_confined=confined), SIGNEDINTAKE)
        note = next(ln for ln in text.splitlines() if "did not leave the file" in ln)
        self.assertLess(len(note), 300, note)
        self.assertIn(f"… {40 - hook.MAX_CONFINED} more", note)

    def test_closure_confined_is_not_also_printed_via_warnings(self):
        # select() no longer puts this text on `warnings` at all (it's
        # structural data on `closure_confined` only) -- confirm the render
        # path doesn't print it twice even if a caller's `warnings` happens
        # to mention the same file for an unrelated reason.
        text = hook.render(
            _result(
                1,
                closure_confined=["app/leaf.py"],
                warnings=["app/leaf.py: registry not approved"],
            ),
            SIGNEDINTAKE,
        )
        self.assertEqual(text.count("app/leaf.py"), 2)  # NOTE line + WARN line, not 3

    def test_closure_confined_survives_the_warning_cap(self):
        # issue #63's whole point is a signal that must not go silent. Riding
        # the capped `warnings` channel like other detail does would let
        # enough queued-ahead warnings push it past MAX_WARNINGS and off the
        # rendered push output entirely.
        text = hook.render(
            _result(
                1,
                closure_confined=["app/leaf.py"],
                warnings=[f"w{i}" for i in range(hook.MAX_WARNINGS + 5)],
            ),
            SIGNEDINTAKE,
        )
        self.assertIn("app/leaf.py", text)
        self.assertIn("did not leave the file", text)

    def test_caps_warnings_without_hiding_the_count(self):
        text = hook.render(
            _result(1, warnings=[f"w{i}" for i in range(9)]), SIGNEDINTAKE
        )
        self.assertIn(f"… {9 - hook.MAX_WARNINGS} more", text)

    def test_blocked_shows_why_and_claims_no_answer(self):
        text = hook.render(
            {"status": "BLOCKED", "base": "a" * 40, "head": "HEAD",
             "blocking": ["schema pin 8 != 9"]},
            SIGNEDINTAKE,
        )
        self.assertIn("no answer", text)
        self.assertIn("schema pin 8 != 9", text)

    def test_abbreviates_shas_but_leaves_symbolic_revs_alone(self):
        text = hook.render(_result(1), SIGNEDINTAKE)
        self.assertIn("aaaaaaaaa..bbbbbbbbb", text)
        self.assertNotIn("a" * 40, text)
        self.assertIn("HEAD~1..HEAD", hook.render(
            _result(1, base="HEAD~1", head="HEAD"), SIGNEDINTAKE))

    def test_no_selection_is_stated_not_blank(self):
        text = hook.render(_result(0), SIGNEDINTAKE)
        self.assertIn("no journeys selected", text)


class NeverBlocksTest(unittest.TestCase):
    """Rule 1: every path exits 0. A hook that can fail a push gets uninstalled,
    which is the state issue #49 exists to end."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _log(self):
        path = os.path.join(self.dir, "ledger.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_exits_zero_when_select_raises(self):
        with mock.patch.object(hook.sel, "select", side_effect=RuntimeError("boom")), \
             mock.patch.object(hook.reg, "resolve_for_repo", return_value="/r.json"), \
             mock.patch.object(hook.os.path, "exists", return_value=True):
            self.assertEqual(hook.main(["--repo", ALPHA, "--base", "HEAD~1"]), 0)
        self.assertEqual(self._log()[0]["status"], "ERROR")
        self.assertIn("boom", self._log()[0]["error"])

    def test_exits_zero_with_no_registry(self):
        with mock.patch.object(hook.reg, "resolve_for_repo", return_value=None):
            self.assertEqual(hook.main(["--repo", GAMMA, "--base", "HEAD~1"]), 0)
        self.assertEqual(self._log()[0]["status"], "NO_REGISTRY")

    def test_exits_zero_with_no_index(self):
        with mock.patch.object(hook.reg, "resolve_for_repo", return_value="/r.json"):
            self.assertEqual(hook.main(["--repo", ALPHA, "--base", "HEAD~1"]), 0)
        self.assertEqual(self._log()[0]["status"], "NO_INDEX")

    def test_an_unwritable_log_still_does_not_fail_the_push(self):
        with mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": "/proc/nope/deeper"}), \
             mock.patch.object(hook.reg, "resolve_for_repo", return_value=None):
            self.assertEqual(hook.main(["--repo", GAMMA, "--base", "HEAD~1"]), 0)


class InvocationLogTest(unittest.TestCase):
    """#49 closes on a non-zero invocation count, so the count IS the feature."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_appends_one_countable_line_per_run(self):
        with mock.patch.object(hook.reg, "resolve_for_repo", return_value=None):
            for _ in range(3):
                hook.main(["--repo", GAMMA, "--base", "HEAD~1"])
        with open(os.path.join(self.dir, "ledger.jsonl")) as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(lines), 3)

    def test_records_what_the_selector_answered(self):
        with mock.patch.object(hook.reg, "resolve_for_repo", return_value="/r.json"), \
             mock.patch.object(hook.os.path, "exists", return_value=True), \
             mock.patch.object(hook.sel, "select", return_value=_result(2)):
            hook.main(["--repo", ALPHA, "--base", "HEAD~1", "--caller", "manual"])
        rec = json.loads(open(os.path.join(self.dir, "ledger.jsonl")).read())
        self.assertEqual(rec["repo"], "alpha")
        self.assertEqual(rec["status"], "OK")
        self.assertEqual(rec["n_journeys"], 2)
        self.assertEqual(rec["journey_ids"], ["J0", "J1"])
        self.assertEqual(rec["caller"], "manual")
        self.assertIn("duration_ms", rec)

    def test_a_symbolic_base_is_resolved_into_its_own_join_key(self):
        # `base` is stored as the caller SPELLED it, which is what the rendered
        # output wants. But the baseline check joins against outcome rows keyed
        # on resolved shas, so a symbolic base matched nothing and every failure
        # on such a push fell to `unbaselined` — pinning observed_recall at None
        # for any caller that passes one. `head` was resolved from the start;
        # `base` was not, and only started mattering once the baseline gated the
        # score.
        head, base = "a" * 40, "b" * 40

        def fake_resolve(repo, rev):
            return {"HEAD": head, "HEAD~1": base}.get(rev)

        with mock.patch.object(hook.reg, "resolve_for_repo", return_value=None), \
             mock.patch.object(hook.ledger, "resolve_commit", side_effect=fake_resolve):
            hook.main(["--repo", GAMMA, "--base", "HEAD~1", "--head", "HEAD"])
        rec = json.loads(open(os.path.join(self.dir, "ledger.jsonl")).read())
        self.assertEqual(rec["base"], "HEAD~1")       # kept for rendering
        self.assertEqual(rec["base_commit"], base)     # the actual join key
        self.assertEqual(rec["commit"], head)


if __name__ == "__main__":
    unittest.main()
