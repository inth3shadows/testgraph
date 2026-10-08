"""CI workflow contract (audit M4). Text-level on purpose: the package is
stdlib-only, so no YAML parser is assumed."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, ".github", "workflows", name)) as fh:
        return fh.read()


class TestWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = _read("test.yml")

    def test_matrix_covers_every_supported_python(self):
        m = re.search(r"python-version:\s*\[([^\]]*)\]", self.text)
        self.assertIsNotNone(m, "unittest job has no python-version matrix")
        for v in ("3.11", "3.12", "3.13"):
            self.assertIn(f'"{v}"', m.group(1))

    def test_a_job_runs_the_suite_with_pytest_installed(self):
        self.assertIn("unittest-with-pytest:", self.text)
        self.assertIn("pip install pytest", self.text)

    def test_every_action_stays_pinned_to_a_commit_sha(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
