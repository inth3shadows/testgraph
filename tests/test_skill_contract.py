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

A third pass then mutation-tested the guards themselves and found five of seven
green against the very defect they name. The evasions were all shape, not
substance, so the parsing is now deliberate:

  * `\\`-continued shell commands are joined before scanning, and argument runs
    are separated by `[ \\t]` rather than `\\s`, so a match neither stops at a
    line continuation (which hid `--base`/`--head` on SKILL.md's primary
    invocation) nor runs past the end of the command into the next markdown line.
  * flags are matched with one *or* two dashes, because `INVOCATION` accepts both
    and a documented `-r` was previously captured and never validated.
  * the escalation table is bounded by its own rows, not by the next `###` — a
    literal "###" inside a row truncated it, and an unrelated markdown table
    later in the document could backfill the row count.
  * assertions target the sentence that carries the instruction, not a phrase
    that may appear anywhere, and compare whitespace-collapsed text so a
    re-wrapped paragraph is not a failure.
"""
import contextlib
import io
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.join(ROOT, "skills", "testgraph-verify", "SKILL.md")

# Argument runs use [ \t], never \s: with \s the group ate the newline and kept
# matching into the following markdown line.
INVOCATION = re.compile(
    r"python3 -m (testgraph\.\w+)((?:[ \t]+-{1,2}[\w-]+(?:[ \t]+\S+)?)*)"
)
FLAG = re.compile(r"-{1,2}[\w-]+")
# `--name-only` must be followed by something naming HEAD; the bare form reports
# only unstaged work.
NAME_ONLY = re.compile(r"--name-only(?:[ \t]+(\S+))?")
# `--base HEAD` (not `HEAD~1`) alongside a `--head` is the degenerate range.
DEGENERATE = re.compile(r"--base[ =]HEAD(?![~\w])")


def skill_text():
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


def joined(text):
    """`text` with shell line continuations folded onto one line.

    Command substitutions collapse to a placeholder: the flags inside
    `$(git -C <repo> merge-base ...)` belong to git, and reading `-C` as a
    testgraph flag is a false positive, not a documentation defect.
    """
    text = re.sub(r"\\\n[ \t]*", " ", text)
    return re.sub(r"\$\([^()]*\)", "SUBST", text)


def squeezed(text):
    """`text` with all whitespace runs collapsed, so a re-wrap is not a diff."""
    return re.sub(r"\s+", " ", text)


def escalation_rows(text):
    """The escalation table's content rows, bounded by the table itself.

    Splitting on the next `###` truncated the table at row 4, which contains a
    literal "`###` section" — so the guards silently inspected a subset, and a
    reordered table failed while intact. Bounding on the row count instead also
    stops an unrelated markdown table further down the document from backfilling
    the count.
    """
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("| Situation |"))
    rows = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        # separator row: every cell is dashes/colons. A row whose PROSE contains
        # "---" is content and must not be dropped.
        if all(re.fullmatch(r"[:-]+", c or "-") for c in cells):
            continue
        rows.append(line)
    return rows


class SkillCommandsExist(unittest.TestCase):
    """Every flag the skill tells an agent to type must actually parse."""

    def test_every_documented_invocation_parses(self):
        import argparse
        import importlib

        text = joined(skill_text())
        found = 0
        for module_name, argstr in INVOCATION.findall(text):
            mod = importlib.import_module(module_name)
            # Rebuild the module's parser by calling main() with --help suppressed
            # is fragile; instead assert each flag appears in its argparse spec.
            parser_flags = set()
            for action in _parser_for(mod, argparse, module_name)._actions:
                parser_flags.update(action.option_strings)
            for flag in FLAG.findall(argstr):
                found += 1
                self.assertIn(
                    flag, parser_flags,
                    f"SKILL.md tells the agent to pass {flag} to {module_name}, "
                    f"but that module accepts only {sorted(parser_flags)}",
                )
        # the feature-branch invocation alone carries three flags across a
        # continuation; a lower count means the regex stopped early again.
        self.assertGreaterEqual(
            found, 6, f"only {found} flags checked — invocation regex drifted"
        )


def _parser_for(mod, argparse, module_name="<module>"):
    """Extract the ArgumentParser a module's main() builds, without running it."""
    captured = {}
    real_init = argparse.ArgumentParser.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.setdefault("p", self)

    if not hasattr(mod, "main"):
        raise AssertionError(
            f"SKILL.md documents `python3 -m {module_name}`, but that module has "
            f"no main() — the command as written is not runnable"
        )
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
    if "p" not in captured:
        raise AssertionError(
            f"SKILL.md documents `python3 -m {module_name}`, but its main() built "
            f"no ArgumentParser, so its flags cannot be verified"
        )
    return captured["p"]


class SkillRulesAreSafe(unittest.TestCase):
    def setUp(self):
        self.text = skill_text()
        self.joined = joined(self.text)
        self.squeezed = squeezed(self.text)

    def test_no_degenerate_diff_range_outside_a_warning(self):
        # `--base HEAD --head <branch>` on that branch is an empty diff -> a
        # confident NONE. It may only appear as an explicit prohibition.
        # Scanned on the CONTINUATION-JOINED text: documented across a `\` the
        # per-physical-line check missed it entirely, and `--base=HEAD` evaded
        # the whitespace-exact substring.
        checked = 0
        for line in self.joined.splitlines():
            if DEGENERATE.search(line) and "--head" in line:
                checked += 1
                self.assertRegex(
                    line.lower(), r"do \*?\*?not|don't|never",
                    f"degenerate range documented as an instruction: {line!r}",
                )
        self.assertGreater(
            checked, 0, "the prohibition itself vanished from the document"
        )

    def test_changed_files_command_includes_staged_work(self):
        self.assertIn("git diff --name-only HEAD", self.text)
        # The bare form misses anything already `git add`ed. Checked at EVERY
        # occurrence, not just the backtick-wrapped spelling — the previous
        # regex ignored the copy-pasteable form inside a ```bash fence.
        for m in NAME_ONLY.finditer(self.joined):
            arg = m.group(1) or ""
            self.assertIn(
                "HEAD", arg,
                f"`--name-only {arg}`.strip() names no commit, so it reports only "
                f"unstaged work: {self.joined[max(0, m.start() - 60):m.end()]!r}",
            )

    def test_non_python_row_matches_what_select_actually_does(self):
        # This test previously asserted "Do NOT rely on `select`" — true before
        # #21, false after it, and asserting it *pinned* the stale instruction so
        # the doc could not be corrected without editing the test (issue #30). It
        # now asserts the current contract AND that the dead wording is gone: the
        # drift-pin points at the code's behavior, not at a past snapshot of it.
        rows = escalation_rows(self.text)
        table = "\n".join(rows)
        self.assertRegex(table, r"non-`?\.py`?", "no non-Python row in the table")
        self.assertNotRegex(
            table, r"[Nn]OT rely on `select`|blind to non-Python",
            "table still tells the agent select ignores non-Python paths (#21 fixed it)",
        )
        row = next(r for r in rows if re.search(r"non-`?\.py`?", r))
        self.assertIn("select", row, "non-Python row names no tool to run")
        self.assertIn(
            "RECALL DEGRADED", row,
            "non-Python row must name the signal that its answer is unknown",
        )

    def test_none_is_qualified_by_the_degrade_signal(self):
        # An extension select accepts but the index does not cover yields zero
        # seeds; #29 made that degrade loudly, and the trust rule must key off
        # that signal rather than "the diff contained .py".
        rules = squeezed(self.text.split("## Rules")[1])
        self.assertIn("RECALL DEGRADED", rules)
        self.assertNotRegex(
            rules, r"only trustworthy for a non-empty, all-Python diff",
            "trust rule still keyed to an all-Python diff (#30)",
        )

    def test_rows_are_matched_by_symbol_not_by_line(self):
        # issue #24: line ranges are frozen at the generation commit while the
        # agent's own edit has already shifted them, so an insertion higher up the
        # file makes a range point at the wrong symbol. An earlier draft told the
        # agent to read "the rows whose line range overlaps your edit" — the rule
        # is correct now, and this is what stops it regressing.
        self.assertIn("Match rows by symbol name first", self.squeezed)
        self.assertIn("Line ranges are a *hint only*", self.squeezed)
        self.assertNotRegex(
            self.squeezed, r"rows? whose line range overlaps",
            "the skill is back to telling the agent to match by line number (#24)",
        )

    def test_absence_rule_keeps_its_qualifier(self):
        # the original #18 defect was an unqualified "absent -> no journeys".
        # Matched on whitespace-collapsed text: the exact substring ended at the
        # document's hand-wrap column, so re-flowing an intact rule failed.
        self.assertIn(
            'only "no journeys" when the map covers its file and lists other '
            'symbols from it', self.squeezed,
        )
        # and the qualifier must say what the other case IS. A bare
        # `"unknown" in text` was vacuous — the word appears four times in
        # unrelated prose and survived stripping the rule entirely.
        self.assertIn("Any other absence is *unknown*", self.squeezed)

    def test_select_is_documented_as_committed_history_only(self):
        # anchored to the sentence that carries the instruction: the loose phrase
        # matched anywhere in the document, so deleting the actual warning and
        # leaving "committed history only" in a sentence about the MAP passed.
        self.assertIn(
            "`select` reads **committed history only** — it cannot see "
            "uncommitted edits, so commit or stash first", self.squeezed,
        )

    def test_every_escalation_row_names_an_action(self):
        rows = escalation_rows(self.text)
        self.assertGreaterEqual(len(rows), 5, "escalation table lost rows")
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertTrue(
                len(cells) >= 2 and cells[1],
                f"escalation row with no action: {row!r}",
            )


if __name__ == "__main__":
    unittest.main()
