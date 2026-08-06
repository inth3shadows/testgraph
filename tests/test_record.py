"""The results ledger and `testgraph record` (issue #10).

The feature is not "a log file". It is one number: **did a journey fail on a
commit whose selection did not name it**. Every test here exists to keep that
number honest, and the honest version turned out to need four buckets, not
three — see `ledger.summarize`. The easy bugs all have the same shape: some
row that says nothing about the selector gets counted as evidence against it.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from testgraph import ledger
from testgraph import record


def sha(n):
    """A 40-hex commit key. The join accepts nothing else — a rev that git
    could not resolve used to be stored verbatim, and two unrelated commits
    then met under the key "HEAD"."""
    return f"{n:040x}"


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
        self.clock = 0

    def _tick(self):
        self.clock += 1
        return self.clock

    def _select(self, commit, journey_ids, base=None, repo="alpha", status="OK", ts=None):
        ledger.append(
            {
                "kind": ledger.SELECTION,
                "ts": self._tick() if ts is None else ts,
                "repo": repo,
                "commit": commit,
                "base": base,
                "status": status,
                "journey_ids": list(journey_ids),
            }
        )

    def _ran(self, commit, journey, verdict, repo="alpha", ts=None):
        ledger.append(
            ledger.outcome_row(
                repo, commit, journey, verdict, ts=self._tick() if ts is None else ts
            )
        )

    def _push(self, base, head, named, repo="alpha"):
        """A push whose selection ANSWERED. It records no baseline — callers that
        need one write the `pass` at `base` themselves.

        The docstring used to claim it recorded a green baseline, which it never
        did. That was not cosmetic: it made `RankingGateTest` read as twenty
        properly-baselined judged failures when it was twenty always-red
        commits with no baseline anywhere, and that test was the thing standing
        behind the ranking gate."""
        self._select(head, named, base=base, repo=repo)


class RoundTripTest(LedgerBase):
    def test_a_written_outcome_reads_back(self):
        with mock.patch.object(ledger, "resolve_commit", return_value=sha(0xDEAD)):
            row, err = record.add_outcome(
                self.repo, "J2", "fail", note="null in the mapper",
                registry_path=self.registry,
            )
        self.assertIsNone(err)
        rows = ledger.read("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], ledger.OUTCOME)
        self.assertEqual(rows[0]["journey"], "J2")
        self.assertEqual(rows[0]["note"], "null in the mapper")

    def test_rows_from_another_repo_are_not_mixed_in(self):
        self._ran(sha(1), "J1", "fail")
        self._ran(sha(1), "J1", "fail", repo="beta")
        self.assertEqual(len(ledger.read("alpha")), 1)
        self.assertEqual(len(ledger.read()), 2)

    def test_an_unresolvable_rev_is_refused_rather_than_stored_verbatim(self):
        # `--repo <the bare-worktree PARENT>` is a directory whose repo_name
        # still resolves, so every other check passes. Storing "HEAD" as the key
        # made two unrelated commits' failures join to each other.
        with mock.patch.object(ledger, "resolve_commit", return_value=None):
            row, err = record.add_outcome(
                self.repo, "J1", "fail", registry_path=self.registry
            )
        self.assertIsNone(row)
        self.assertIn("cannot resolve", err)
        self.assertEqual(ledger.read("alpha"), [])


class TheJoinTest(LedgerBase):
    """`caught` / `missed` / `unasked` / `unbaselined` — why this ledger exists."""

    def test_a_failure_the_selection_named_is_caught(self):
        self._ran(sha(1), "J1", "pass")            # green at the base
        self._push(base=sha(1), head=sha(2), named=["J1", "J2"])
        self._ran(sha(2), "J1", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (1, 0))
        self.assertEqual(s["observed_recall"], 1.0)

    def test_a_failure_the_selection_did_not_name_is_a_silent_miss(self):
        self._ran(sha(1), "J2", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 1))
        self.assertEqual(s["observed_recall"], 0.0)
        self.assertEqual(s["journeys"]["J2"]["missed"], 1)

    def test_a_failure_on_an_unanswered_commit_is_unasked_not_missed(self):
        self._ran(sha(1), "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertIsNone(s["observed_recall"])
        self.assertEqual(s["journeys"]["J2"]["unasked"], 1)

    def test_a_catch_with_no_green_baseline_is_not_credited_to_the_selector(self):
        # The mirror of the test below, and the defect it pins is the same one
        # pointed the other way: a row that says nothing about the selector
        # counted as evidence — there against it, here FOR it.
        #
        # J1 is red from the first push and never recorded green. Two pushes
        # touch code reaching it and the selection names it both times. Naming an
        # already-red journey predicts nothing, so crediting it made pre-existing
        # breakage able to raise observed_recall and never able to lower it: this
        # history scored 2 caught / 0 missed / recall 1.00, while the identical
        # history with a selector that named NOTHING was excluded from scoring
        # entirely. A perfect score for zero information.
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J1", "fail")
        self._push(base=sha(2), head=sha(3), named=["J1"])
        self._ran(sha(3), "J1", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertIsNone(s["observed_recall"])
        self.assertEqual(s["journeys"]["J1"]["unbaselined"], 2)

    def test_the_baseline_gates_catches_and_misses_identically(self):
        # Same push, same base, same green baseline — the ONLY difference is
        # whether the selection named J1. One is a catch, the other a miss, and
        # both are scored. Asymmetry here is what the two tests around this one
        # exist to prevent.
        self._ran(sha(1), "J1", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J1", "fail")
        self._ran(sha(3), "J2", "pass")
        self._push(base=sha(3), head=sha(4), named=["J1"])
        self._ran(sha(4), "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (1, 1))
        self.assertEqual(s["observed_recall"], 0.5)

    def test_a_failure_with_no_green_baseline_is_not_blamed_on_the_selector(self):
        # The trace that used to score a false miss: A..B breaks J3 and the
        # selection for B NAMES it; nobody runs journeys; B..C touches only
        # README so its selection names nothing; the developer runs J3 at HEAD
        # (=C) per USAGE.md and it fails. The selector was right both times.
        self._push(base=sha(1), head=sha(2), named=["J3"])
        self._push(base=sha(2), head=sha(3), named=[])
        self._ran(sha(3), "J3", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertIsNone(s["observed_recall"])
        self.assertEqual(s["journeys"]["J3"]["unbaselined"], 1)

    def test_a_selection_that_declined_to_answer_is_not_evidence(self):
        # hook.run writes rows with no journey_ids on NO_REGISTRY, NO_INDEX,
        # ERROR and BLOCKED. Counting those as "asked and answered nothing" made
        # a tripped integrity guard — or one JSON typo in an approved registry —
        # score every later failure as the selector's fault.
        for status in ("BLOCKED", "ERROR", "NO_REGISTRY", "NO_INDEX"):
            with self.subTest(status=status):
                self.setUp()
                self._ran(sha(1), "J1", "pass")
                self._select(sha(2), [], base=sha(1), status=status)
                self._ran(sha(2), "J1", "fail")
                s = ledger.summarize("alpha")
                self.assertEqual(s["missed"], 0)
                self.assertEqual(s["journeys"]["J1"]["unasked"], 1)

    def test_recall_is_the_ratio_across_judged_failures_only(self):
        self._ran(sha(1), "J1", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J1", "fail")              # caught
        self._ran(sha(3), "J2", "pass")
        self._push(base=sha(3), head=sha(4), named=["J1"])
        self._ran(sha(4), "J2", "fail")              # missed
        self._ran(sha(5), "J3", "fail")              # unasked
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (1, 1))
        self.assertEqual(s["observed_recall"], 0.5)

    def test_a_pass_is_never_a_miss_however_the_selection_answered(self):
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J2", "pass")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertEqual(s["journeys"]["J2"]["passes"], 1)

    def test_repeated_pushes_of_one_commit_union_their_selections(self):
        self._ran(sha(1), "J2", "pass")
        self._select(sha(2), ["J1"], base=sha(1))
        self._select(sha(2), ["J2"], base=sha(1))
        self._ran(sha(2), "J2", "fail")
        self.assertEqual(ledger.summarize("alpha")["missed"], 0)

    def test_recording_one_failure_twice_is_one_observation(self):
        # A re-run to confirm, or /autorun logging each attempt, used to
        # multiply the headline miss count for a single defect — while
        # judged_commits stayed deduplicated, so the two numbers disagreed.
        self._ran(sha(1), "J2", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J2", "fail")
        self._ran(sha(2), "J2", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual(s["missed"], 1)
        # One failure, not two: the pass at the base and the fail at the head
        # are two observations; the duplicated fail is one.
        self.assertEqual(s["journeys"]["J2"]["failures"], 1)
        self.assertEqual(s["journeys"]["J2"]["runs"], 2)


class RankingGateTest(LedgerBase):
    def test_ranking_stays_off_until_enough_commits_are_judged(self):
        # Each push needs its own green baseline. Without one these are twenty
        # observations of an always-red journey, which is not judged evidence at
        # all — see test_always_red_history_cannot_open_the_gate below, which is
        # what this fixture accidentally used to be.
        for i in range(ledger.MIN_JUDGED_COMMITS - 1):
            self._ran(sha(i), "J1", "pass")
            self._push(base=sha(i), head=sha(1000 + i), named=["J1"])
            self._ran(sha(1000 + i), "J1", "fail")
        self.assertFalse(ledger.summarize("alpha")["ready_for_ranking"])
        self._ran(sha(500), "J1", "pass")
        self._push(base=sha(500), head=sha(999), named=["J1"])
        self._ran(sha(999), "J1", "fail")
        self.assertTrue(ledger.summarize("alpha")["ready_for_ranking"])

    def test_always_red_history_cannot_open_the_gate(self):
        # The same twenty pushes with no baseline anywhere. Every failure is
        # `unbaselined`, so nothing is judged and the gate stays shut. This is
        # the state the fixture above was silently in.
        for i in range(ledger.MIN_JUDGED_COMMITS + 5):
            self._push(base=sha(i), head=sha(1000 + i), named=["J1"])
            self._ran(sha(1000 + i), "J1", "fail")
        s = ledger.summarize("alpha")
        self.assertEqual((s["caught"], s["missed"]), (0, 0))
        self.assertFalse(s["ready_for_ranking"])

    def test_skipped_journeys_cannot_satisfy_the_gate(self):
        # `skip` says the journey was deliberately NOT run. Twenty of those used
        # to flip the flag while carrying zero failure evidence — the exact
        # quantity the threshold reasons about.
        for i in range(ledger.MIN_JUDGED_COMMITS + 5):
            self._push(base=sha(i), head=sha(1000 + i), named=["J1"])
            self._ran(sha(1000 + i), "J1", "skip")
        s = ledger.summarize("alpha")
        self.assertEqual(s["judged_commits"], 0)
        self.assertFalse(s["ready_for_ranking"])

    def test_history_without_a_judged_failure_does_not_flip_the_gate(self):
        for i in range(ledger.MIN_JUDGED_COMMITS + 5):
            self._push(base=sha(i), head=sha(1000 + i), named=["J1"])
            self._ran(sha(1000 + i), "J1", "pass")
        s = ledger.summarize("alpha")
        self.assertGreaterEqual(s["judged_commits"], ledger.MIN_JUDGED_COMMITS)
        self.assertFalse(s["ready_for_ranking"])

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

    def test_an_unparseable_registry_is_not_reported_as_a_missing_one(self):
        # json.JSONDecodeError is a ValueError, so a trailing comma surfaced as
        # "draft one" — telling the user to write a file they already have, and
        # hiding that the same typo has silently disabled the pre-push hook.
        broken = os.path.join(self.root, "broken.json")
        with open(broken, "w") as f:
            f.write('{"target": "alpha", "journeys": {,}}')
        known, why = record.known_journeys(self.repo, broken)
        self.assertIsNone(known)
        self.assertIn("will not parse", why)
        self.assertIn("pre-push hook", why)
        self.assertNotIn("draft one", why)

    def test_the_cli_exits_nonzero_on_a_bad_journey(self):
        rc = record.main(
            ["--repo", self.repo, "--journey", "J9", "--outcome", "fail",
             "--registry", self.registry]
        )
        self.assertEqual(rc, 2)


class CommitKeyTest(LedgerBase):
    def test_a_non_sha_selection_key_is_not_joinable(self):
        # A manual `hook.main --head HEAD` run wrote the literal string. Letting
        # it into the join made every such row collide.
        self._select("HEAD", ["J1"], base=sha(1))
        self._ran(sha(2), "J1", "fail")
        self.assertEqual(ledger.summarize("alpha")["journeys"]["J1"]["unasked"], 1)

    def test_a_legacy_row_joins_on_its_head_sha(self):
        # The pre-rename hook wrote {ts, repo, base, head, status, journey_ids…}
        # with NO `commit` key, and git handed it a full sha. That fallback is
        # the only thing making a pre-rename install's history joinable.
        with open(os.path.join(self.state, ledger.LEGACY_NAME), "w") as f:
            f.write(json.dumps({
                "ts": 1, "repo": "alpha", "base": sha(1), "head": sha(2),
                "status": "OK", "journey_ids": ["J1"],
            }) + "\n")
        self._ran(sha(1), "J1", "pass", ts=2)
        self._ran(sha(2), "J1", "fail", ts=3)
        s = ledger.summarize("alpha")
        self.assertEqual(s["selections"], 1)
        self.assertEqual(s["caught"], 1)


class DurabilityTest(LedgerBase):
    def test_a_torn_line_does_not_blind_the_reader(self):
        self._ran(sha(1), "J1", "fail")
        with open(ledger.path(self.state), "a") as f:
            f.write('{"kind": "outcome", "repo": "alp\n')
        self._ran(sha(2), "J2", "fail")
        self.assertEqual(len(ledger.read("alpha")), 2)

    def test_a_non_numeric_timestamp_does_not_crash_the_reader(self):
        # Valid JSON, a dict, past every other guard — and `sort(key=ts or 0)`
        # raised TypeError. The KB export loop invites an agent to write here.
        with open(ledger.path(self.state), "a") as f:
            f.write(json.dumps({
                "kind": "outcome", "ts": "2026-08-06", "repo": "alpha",
                "commit": sha(1), "journey": "J1", "verdict": "fail",
            }) + "\n")
        self._ran(sha(2), "J2", "fail")
        self.assertEqual(len(ledger.read("alpha")), 2)
        self.assertEqual(ledger.summarize("alpha")["outcomes"], 2)

    def test_an_unwritable_state_dir_returns_false_rather_than_raising(self):
        with mock.patch.dict(os.environ, {"TESTGRAPH_STATE_DIR": "/proc/nope/deeper"}):
            self.assertFalse(ledger.append({"kind": "outcome", "repo": "alpha"}))


class KbExportTest(LedgerBase):
    def test_the_payload_carries_the_counts_and_defers_the_table(self):
        self._ran(sha(1), "J1", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J1", "fail")
        payload = record.kb_payload(ledger.summarize("alpha"))
        self.assertIn("alpha", payload["proposal"]["content"])
        evidence = json.loads(payload["proposal"]["evidence"])
        self.assertEqual(evidence["caught"], 1)
        self.assertEqual(evidence["observed_recall"], 1.0)
        self.assertNotIn("table", payload["proposal"])
        self.assertIn("kb.read.search", payload["next"])


class RenderTest(LedgerBase):
    def test_a_silent_miss_is_shouted_not_buried_in_a_column(self):
        self._ran(sha(1), "J2", "pass")
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J2", "fail")
        text = record.render_summary(ledger.summarize("alpha"))
        self.assertIn("SILENT MISS", text)

    def test_an_unbaselined_failure_says_why_it_was_not_judged(self):
        self._push(base=sha(1), head=sha(2), named=[])
        self._ran(sha(2), "J3", "fail")
        text = record.render_summary(ledger.summarize("alpha"))
        self.assertIn("NOT judged", text)
        self.assertNotIn("SILENT MISS", text)

    def test_the_summary_names_journeys_from_the_registry(self):
        self._push(base=sha(1), head=sha(2), named=["J1"])
        self._ran(sha(2), "J1", "pass")
        text = record.render_summary(
            ledger.summarize("alpha"),
            record.known_journeys(self.repo, self.registry)[0],
        )
        self.assertIn("journey J1", text)


if __name__ == "__main__":
    unittest.main()
