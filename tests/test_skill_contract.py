"""Executable checks over `skills/testgraph-verify/SKILL.md`.

Two independent review passes each found the same class of bug: prose telling an
agent to run something that does not work, or to conclude something unsafe. Both
slipped through because the skill was the only part of this project with no
verification behind it — the rules are instructions to a model, so nothing failed
when they were wrong.

These tests make the document falsifiable. They do not check style; each one pins
a specific defect that shipped:

  * an escalation command whose flags do not exist (would have caught a rename)
  * `--base HEAD --head <branch>`, which yields an empty diff and a confident
    `NONE` (shipped in #26, caught by review)
  * `git diff --name-only` without `HEAD`, which misses staged work (same)
  * a non-`.py` escalation row that outlived the code it described — it told
    agents `select` was blind to their file long after #21 taught it to seed
    those paths, and the test asserting that row pinned the staleness in place
    (#30). The row must now agree with the selector's behavior, and `NONE` must
    be qualified by the `RECALL DEGRADED` signal (#29) rather than by extension.
  * the absence rule losing its qualifier, which is the original #18 defect
"""
import contextlib
import io
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.join(ROOT, "skills", "testgraph-verify", "SKILL.md")

INVOCATION = re.compile(r"python3 -m (testgraph\.\w+)((?:\s+--?[\w-]+(?:\s+\S+)?)*)")
FLAG = re.compile(r"--[\w-]+")


def skill_text():
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


class SkillCommandsExist(unittest.TestCase):
    """Every flag the skill tells an agent to type must actually parse."""

    def test_every_documented_invocation_parses(self):
        import argparse
        import importlib

        text = skill_text()
        found = 0
        for module_name, argstr in INVOCATION.findall(text):
            mod = importlib.import_module(module_name)
            # Rebuild the module's parser by calling main() with --help suppressed
            # is fragile; instead assert each flag appears in its argparse spec.
            parser_flags = set()
            for action in _parser_for(mod, argparse)._actions:
                parser_flags.update(action.option_strings)
            for flag in FLAG.findall(argstr):
                found += 1
                self.assertIn(
                    flag, parser_flags,
                    f"SKILL.md tells the agent to pass {flag} to {module_name}, "
                    f"but that module accepts only {sorted(parser_flags)}",
                )
        self.assertGreater(found, 0, "no invocations found — regex or doc drifted")


def _parser_for(mod, argparse):
    """Extract the ArgumentParser a module's main() builds, without running it."""
    captured = {}
    real_init = argparse.ArgumentParser.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.setdefault("p", self)

    argparse.ArgumentParser.__init__ = spy
    try:
        # --help writes usage to stdout and exits; swallow both so test output
        # stays readable.
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                mod.main(["--help"])
            except SystemExit:
                pass
    finally:
        argparse.ArgumentParser.__init__ = real_init
    return captured["p"]


class SkillRulesAreSafe(unittest.TestCase):
    def setUp(self):
        self.text = skill_text()

    def test_no_degenerate_diff_range_outside_a_warning(self):
        # `--base HEAD --head <branch>` on that branch is an empty diff -> a
        # confident NONE. It may only appear as an explicit prohibition.
        for line in self.text.splitlines():
            if "--base HEAD " in line and "--head" in line:
                self.assertRegex(
                    line.lower(), r"do \*?\*?not|don't|never",
                    f"degenerate range documented as an instruction: {line!r}",
                )

    def test_changed_files_command_includes_staged_work(self):
        self.assertIn("git diff --name-only HEAD", self.text)
        # bare form would miss anything already `git add`ed
        self.assertNotRegex(self.text, r"`git diff --name-only`")

    def test_non_python_row_matches_what_select_actually_does(self):
        # This test previously asserted "Do NOT rely on `select`" — true before
        # #21, false after it, and asserting it *pinned* the stale instruction so
        # the doc could not be corrected without editing the test (issue #30). It
        # now asserts the current contract AND that the dead wording is gone: the
        # drift-pin points at the code's behavior, not at a past snapshot of it.
        table = self.text.split("| Situation |")[1].split("###")[0]
        self.assertRegex(table, r"non-`?\.py`?", "no non-Python row in the table")
        self.assertNotRegex(
            table, r"[Nn]OT rely on `select`|blind to non-Python",
            "table still tells the agent select ignores non-Python paths (#21 fixed it)",
        )
        row = next(
            r for r in table.splitlines()
            if r.startswith("|") and re.search(r"non-`?\.py`?", r)
        )
        self.assertIn("select", row, "non-Python row names no tool to run")
        self.assertIn(
            "RECALL DEGRADED", row,
            "non-Python row must name the signal that its answer is unknown",
        )

    def test_none_is_qualified_by_the_degrade_signal(self):
        # An extension select accepts but the index does not cover yields zero
        # seeds; #29 made that degrade loudly, and the trust rule must key off
        # that signal rather than "the diff contained .py".
        rules = self.text.split("## Rules")[1]
        self.assertIn("RECALL DEGRADED", rules)
        self.assertNotRegex(
            rules, r"only trustworthy for a non-empty, all-Python diff",
            "trust rule still keyed to an all-Python diff (#30)",
        )

    def test_absence_rule_keeps_its_qualifier(self):
        # the original #18 defect was an unqualified "absent -> no journeys".
        self.assertIn("only \"no journeys\" when the map covers its", self.text)
        self.assertIn("unknown", self.text.lower())

    def test_select_is_documented_as_committed_history_only(self):
        self.assertRegex(self.text, r"committed history only")

    def test_every_escalation_row_names_an_action(self):
        rows = [
            r for r in self.text.split("| Situation |")[1].split("\n")
            if r.startswith("|") and "---" not in r
        ]
        self.assertGreaterEqual(len(rows), 5, "escalation table lost rows")
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertTrue(
                len(cells) >= 2 and cells[1],
                f"escalation row with no action: {row!r}",
            )


if __name__ == "__main__":
    unittest.main()
