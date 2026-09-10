"""Phase 3: the normalized result record, the tag convention, and the
`runners:` spec (verification-direction plan, D3). No runner execution here —
that's Phase 4 — just the schema everything else will build on."""
import unittest

from testgraph import results


class RunResultTests(unittest.TestCase):
    def test_minimal_result(self):
        r = results.run_result("pytest", "tests/test_x.py::test_y", "pass")
        self.assertEqual(r["runner"], "pytest")
        self.assertEqual(r["test_id"], "tests/test_x.py::test_y")
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["journeys"], [])
        self.assertEqual(r["artifacts"], {})

    def test_full_result(self):
        r = results.run_result(
            "playwright", "e2e/login.spec.ts:12", "fail",
            journeys=["J4", "J7"], duration_s=1.23, message="timeout",
            artifacts={"trace": "trace.zip"}, ts=1000,
        )
        self.assertEqual(r["journeys"], ["J4", "J7"])
        self.assertEqual(r["duration_s"], 1.23)
        self.assertEqual(r["message"], "timeout")
        self.assertEqual(r["artifacts"], {"trace": "trace.zip"})
        self.assertEqual(r["ts"], 1000)

    def test_journeys_defensive_copy(self):
        # A caller mutating the list they passed in must not mutate the row.
        source = ["J1"]
        r = results.run_result("pytest", "t", "pass", journeys=source)
        source.append("J2")
        self.assertEqual(r["journeys"], ["J1"])

    def test_unknown_status_raises(self):
        with self.assertRaises(ValueError):
            results.run_result("pytest", "t", "passed")  # not "pass"


class ToLedgerVerdictTests(unittest.TestCase):
    def test_error_maps_to_fail(self):
        self.assertEqual(results.TO_LEDGER_VERDICT["error"], "fail")

    def test_every_status_has_a_mapping(self):
        for status in results.STATUSES:
            self.assertIn(status, results.TO_LEDGER_VERDICT)

    def test_every_mapped_value_is_a_real_ledger_verdict(self):
        from testgraph import ledger
        for verdict in results.TO_LEDGER_VERDICT.values():
            self.assertIn(verdict, ledger.VERDICTS)


class TagConventionTests(unittest.TestCase):
    def test_format_tag_pytest(self):
        self.assertEqual(results.format_tag("tg_{journey}", "J4"), "tg_J4")

    def test_format_tag_playwright(self):
        self.assertEqual(results.format_tag("@tg:{journey}", "J4"), "@tg:J4")

    def test_roundtrip_every_default_convention(self):
        for template in results.DEFAULT_TAG_CONVENTIONS.values():
            tag = results.format_tag(template, "J4")
            self.assertEqual(results.parse_journey_from_tag(template, tag), "J4")

    def test_parse_rejects_non_matching_text(self):
        self.assertIsNone(results.parse_journey_from_tag("tg_{journey}", "unrelated"))

    def test_parse_is_a_full_match_not_a_search(self):
        # "tg_J4_extra" must not be misread as journey "J4_extra" silently
        # matching a prefix — a partial match here would join a test to the
        # wrong journey.
        self.assertEqual(
            results.parse_journey_from_tag("tg_{journey}", "tg_J4_extra"), "J4_extra"
        )
        self.assertIsNone(
            results.parse_journey_from_tag("tg_{journey}_suffix", "tg_J4_extra")
        )

    def test_template_without_placeholder_rejected(self):
        with self.assertRaises(ValueError):
            results._template_regex("no-placeholder-here")

    def test_template_with_two_placeholders_rejected(self):
        with self.assertRaises(ValueError):
            results._template_regex("{journey}-{journey}")


class ParseRunnersSpecTests(unittest.TestCase):
    def test_valid_minimal_spec(self):
        parsed, errors = results.parse_runners_spec({
            "pytest": {
                "select": "pytest -m '{marker_or}' --junitxml={out}",
                "tag": "tg_{journey}",
                "results": "junit-xml",
            }
        })
        self.assertEqual(errors, [])
        self.assertIn("pytest", parsed)
        self.assertEqual(parsed["pytest"]["tag"], "tg_{journey}")

    def test_optional_fields_carried_through(self):
        parsed, errors = results.parse_runners_spec({
            "playwright": {
                "select": "npx playwright test --grep '{tag_or}' --reporter=junit",
                "tag": "@tg:{journey}",
                "results": "junit-xml",
                "artifacts": "playwright-trace",
                "adapter": "testgraph.runners.playwright",
            }
        })
        self.assertEqual(errors, [])
        self.assertEqual(parsed["playwright"]["artifacts"], "playwright-trace")
        self.assertEqual(parsed["playwright"]["adapter"], "testgraph.runners.playwright")

    def test_missing_required_field_reported_by_name(self):
        parsed, errors = results.parse_runners_spec({
            "pytest": {"select": "pytest", "tag": "tg_{journey}"}  # no "results"
        })
        self.assertEqual(parsed, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("pytest", errors[0])
        self.assertIn("results", errors[0])

    def test_one_bad_runner_does_not_blind_the_others(self):
        parsed, errors = results.parse_runners_spec({
            "pytest": {
                "select": "pytest -m '{marker_or}'", "tag": "tg_{journey}", "results": "junit-xml",
            },
            "broken": {"select": "x"},  # missing tag, results
        })
        self.assertIn("pytest", parsed)
        self.assertNotIn("broken", parsed)
        self.assertEqual(len(errors), 1)

    def test_non_string_field_rejected(self):
        parsed, errors = results.parse_runners_spec({
            "pytest": {"select": "pytest", "tag": "tg_{journey}", "results": 123}
        })
        self.assertEqual(parsed, {})
        self.assertTrue(errors)

    def test_bad_tag_template_rejected(self):
        parsed, errors = results.parse_runners_spec({
            "pytest": {"select": "pytest", "tag": "no-placeholder", "results": "junit-xml"}
        })
        self.assertEqual(parsed, {})
        self.assertTrue(errors)

    def test_top_level_not_a_mapping(self):
        parsed, errors = results.parse_runners_spec(["not", "a", "dict"])
        self.assertEqual(parsed, {})
        self.assertTrue(errors)

    def test_runner_spec_not_a_mapping(self):
        parsed, errors = results.parse_runners_spec({"pytest": "just a string"})
        self.assertEqual(parsed, {})
        self.assertIn("pytest", errors[0])


if __name__ == "__main__":
    unittest.main()
