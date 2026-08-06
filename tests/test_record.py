"""The results ledger and `testgraph record` (issue #10).

The feature is not "a log file". It is one number: **did a journey fail on a
commit whose selection did not name it**. Every test here exists to keep that
number honest — chiefly by keeping `missed` (the selector was asked and got it
wrong) apart from `unjudged` (the selector was never asked). Collapsing those
two is the easy bug, and it would score every failure recorded before the hook
was installed as a recall miss.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from testgraph import ledger
from testgraph import record


def _repo(root, project="alpha", leaf="main"):
    """`<root>/<project>/{.bare,<leaf>}` — the layout repo-init produces."""
    path = os.path.join(root, project, leaf)
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(root, project, ".bare"), exist_ok=True)
    return path


def _registry(directory, target="alpha", journeys=("J1", "J2", "J3")):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{target}.json")
    with open(path, "w") as f:
        json.dump(
            {
                "target": target,
                "approved": True,
                "journeys": {j: {"name": f"journey {j}", "entries": []} for j in journeys},
            },
            f,
        )
    return path


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = tempfile.mkdtemp()
        self.repo = _repo(self.root)
        self.registry = _registry(os.path.join(self.root, "journeys"))

    def _select(self, commit, journey_ids, repo="alpha", ts=1):
        ledger.append(
            {
                "kind": ledger.SELECTION,
                "ts": ts,
                "repo": repo,
                "commit": commit,
                "status": "OK",
                "journey_ids": list(journey_ids),
            }
        )

    def _ran(self, commit, journey, verdict, repo="alpha", ts=2):
        ledger.append(ledger.outcome_row(repo, commit, journey, verdict, ts=ts))


class RoundTripTest(LedgerBase):
    def test_a_written_outcome_reads_back(self):
        row, err = record.add_outcome(
            self.repo, "J2", "fail", commit="deadbeef", note="null in the mapper",
            registry_path=self.registry,
        )
        self.assertIsNone(err)
        rows = ledger.read("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], ledger.OUTCOME)
        self.assertEqual(rows[0]["journey"], "J2")
        self.assertEqual(rows[0]["verdict"], "fail")
        self.assertEqual(rows[0]["note"], "null in the mapper")

    def test_rows_from_another_repo_are_not_mixed_in(self):
        self._ran("c1", "J1", "fail")
        self._ran("c1", "J1", "fail", repo="beta")
        self.assertEqual(len(ledger.read("alpha")), 1)
        self.assertEqual(len(ledger.read()), 2)

    def test_commit_defaults_to_head_of_the_repo(self):
        with mock.patch.object(ledger, "resolve_commit", return_value="abc123"):
            row, err = record.add_outcome(
                self.repo, "J1", "pass", registry_path=self.registry
            )
        self.assertIsNone(err)
        self.assertEqual(row["commit"], "abc123")


class TheJoinTest(LedgerBase):
    """`caught` / `missed` / `unjudged` — the only reason this ledger exists."""

    def test_a_failure_the_selection_named_is_caught(self):
        self._select("c1", ["J1", "J2"])
        self._ran("c1", "J1", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual(s["caught"], 1)
        self.assertEqual(s["missed"], 0)
        self.assertEqual(s["observed_recall"], 1.0)

    def test_a_failure_the_selection_did_not_name_is_a_silent_miss(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual(s["missed"], 1)
        self.assertEqual(s["caught"], 0)
        self.assertEqual(s["observed_recall"], 0.0)
        self.assertEqual(s["journeys"]["J2"]["missed"], 1)

    def test_a_failure_on_an_unanswered_commit_is_unjudged_not_missed(self):
        # No selection row for c1: testgraph was never asked, so this says
        # nothing about recall. Counting it as a miss is the bug this pins.
        self._ran("c1", "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual(s["missed"], 0)
        self.assertEqual(s["caught"], 0)
        self.assertIsNone(s["observed_recall"])
        self.assertEqual(s["journeys"]["J2"]["unjudged"], 1)

    def test_recall_is_the_ratio_across_judged_failures_only(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J1", "fail")     # caught
        self._select("c2", ["J1"])
        self._ran("c2", "J2", "fail")     # missed
        self._ran("c3", "J3", "fail")     # unjudged — excluded from the ratio
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (1, 1))
        self.assertEqual(s["observed_recall"], 0.5)

    def test_a_pass_is_never_a_miss_however_the_selection_answered(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J2", "pass")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertEqual(s["journeys"]["J2"]["passes"], 1)

    def test_repeated_pushes_of_one_commit_union_their_selections(self):
        # A branch pushed twice answers twice for the same head. A journey named
        # by either answer was named.
        self._select("c1", ["J1"])
        self._select("c1", ["J2"])
        self._ran("c1", "J2", "fail")
        self.assertEqual(ledger.summarize("alpha")["missed"], 0)


class RankingGateTest(LedgerBase):
    def test_ranking_stays_off_until_enough_commits_are_judged(self):
        for i in range(ledger.MIN_JUDGED_COMMITS - 1):
            self._select(f"c{i}", ["J1"])
            self._ran(f"c{i}", "J1", "pass")
        self.assertFalse(ledger.summarize("alpha")["ready_for_ranking"])
        self._select("cX", ["J1"])
        self._ran("cX", "J1", "pass")
        self.assertTrue(ledger.summarize("alpha")["ready_for_ranking"])

    def test_an_empty_ledger_says_so_rather_than_reporting_a_recall(self):
        text = record.render_summary(ledger.summarize("alpha"))
        self.assertIn("no rows yet", text)
        self.assertNotIn("observed recall:", text)


class ValidationTest(LedgerBase):
    def test_an_unknown_journey_id_is_refused(self):
        row, err = record.add_outcome(
            self.repo, "J33", "fail", registry_path=self.registry
        )
        self.assertIsNone(row)
        self.assertIn("unknown journey", err)
        self.assertEqual(ledger.read("alpha"), [])

    def test_an_unknown_verdict_is_refused(self):
        row, err = record.add_outcome(
            self.repo, "J1", "exploded", registry_path=self.registry
        )
        self.assertIsNone(row)
        self.assertIn("unknown outcome", err)

    def test_a_repo_with_no_registry_is_refused_with_the_remedy(self):
        other = _repo(self.root, project="gamma")
        row, err = record.add_outcome(other, "J1", "fail")
        self.assertIsNone(row)
        self.assertIn("no journey registry", err)
        self.assertIn("testgraph.propose", err)

    def test_the_cli_exits_nonzero_on_a_bad_journey(self):
        rc = record.main(
            ["--repo", self.repo, "--journey", "J9", "--outcome", "fail",
             "--registry", self.registry]
        )
        self.assertEqual(rc, 2)


class DurabilityTest(LedgerBase):
    def test_a_torn_line_does_not_blind_the_reader(self):
        # A push killed mid-write leaves a partial line. Everything after it
        # must still be readable.
        self._ran("c1", "J1", "fail")
        with open(ledger.path(self.state), "a") as f:
            f.write('{"kind": "outcome", "repo": "alp\n')
        self._ran("c2", "J2", "fail")
        self.assertEqual(len(ledger.read("alpha")), 2)

    def test_an_unwritable_state_dir_returns_false_rather_than_raising(self):
        with mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": "/proc/nope/deeper"}):
            self.assertFalse(ledger.append({"kind": "outcome", "repo": "alpha"}))

    def test_legacy_invocation_rows_are_read_as_selections(self):
        # The pre-push hook wrote `invocations.jsonl` before this module existed.
        # Renaming must not silently drop an install's history.
        with open(os.path.join(self.state, ledger.LEGACY_NAME), "w") as f:
            f.write(json.dumps({"ts": 1, "repo": "alpha", "commit": "c1",
                                "journey_ids": ["J1"]}) + "\n")
        self._ran("c1", "J1", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual(s["selections"], 1)
        self.assertEqual(s["caught"], 1)


class KbExportTest(LedgerBase):
    def test_the_payload_carries_the_counts_and_defers_the_table(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J1", "fail")
        payload = record.kb_payload(ledger.summarize("alpha"))
        self.assertIn("alpha", payload["proposal"]["content"])
        evidence = json.loads(payload["proposal"]["evidence"])
        self.assertEqual(evidence["caught"], 1)
        self.assertEqual(evidence["observed_recall"], 1.0)
        # The table is chosen by an agent after kb.read.search, not hardcoded.
        self.assertNotIn("table", payload["proposal"])
        self.assertIn("kb.read.search", payload["next"])


class RenderTest(LedgerBase):
    def test_a_silent_miss_is_shouted_not_buried_in_a_column(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J2", "fail")
        text = record.render_summary(ledger.summarize("alpha"))
        self.assertIn("SILENT MISS", text)

    def test_the_summary_names_journeys_from_the_registry(self):
        self._select("c1", ["J1"])
        self._ran("c1", "J1", "pass")
        text = record.render_summary(
            ledger.summarize("alpha"), record.known_journeys(self.repo, self.registry)
        )
        self.assertIn("journey J1", text)


if __name__ == "__main__":
    unittest.main()
