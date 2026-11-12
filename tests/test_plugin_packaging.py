"""The trace plugin ships in the wheel: `verify` failed after `pip install` (audit 2026-11-12)."""
import os
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from testgraph import pytest_adapter as pa  # noqa: E402


class PluginPackaging(unittest.TestCase):
    def test_wheel_force_includes_the_plugin_from_its_source_of_truth(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            conf = tomllib.load(f)
        inc = conf["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(inc, {"harness/plugin/tgtrace.py": "testgraph/_plugin/tgtrace.py"})
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "harness/plugin/tgtrace.py")))

    def test_an_installed_package_finds_the_packaged_plugin(self):
        with tempfile.TemporaryDirectory() as site:
            pkg = os.path.join(site, "testgraph")
            os.makedirs(os.path.join(pkg, "_plugin"))
            open(os.path.join(pkg, "_plugin", "tgtrace.py"), "w").close()
            with mock.patch.object(pa, "__file__", os.path.join(pkg, "pytest_adapter.py")):
                self.assertEqual(pa.plugin_dir(), os.path.join(pkg, "_plugin"))

    def test_a_checkout_uses_harness_plugin(self):
        self.assertEqual(pa.plugin_dir(), os.path.join(ROOT, "harness", "plugin"))

    def test_a_stray_packaged_copy_in_a_checkout_does_not_shadow_the_source(self):
        with tempfile.TemporaryDirectory() as repo:
            for d in ("harness/plugin", "testgraph/_plugin"):
                os.makedirs(os.path.join(repo, d))
                open(os.path.join(repo, d, "tgtrace.py"), "w").close()
            fake = os.path.join(repo, "testgraph", "pytest_adapter.py")
            with mock.patch.object(pa, "__file__", fake):
                self.assertEqual(pa.plugin_dir(), os.path.join(repo, "harness", "plugin"))

    def test_the_sdist_carries_every_file_the_wheel_force_includes(self):
        # `uv build` builds the wheel FROM the sdist (#165 review, B1).
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            targets = tomllib.load(f)["tool"]["hatch"]["build"]["targets"]
        include = targets["sdist"]["include"]
        for src in targets["wheel"]["force-include"]:
            self.assertTrue(any(src == i or src.startswith(i.rstrip("/") + "/")
                                for i in include), src)

    def test_neither_present_is_none_not_a_guess(self):
        with tempfile.TemporaryDirectory() as site:
            pkg = os.path.join(site, "testgraph")
            os.makedirs(pkg)
            with mock.patch.object(pa, "__file__", os.path.join(pkg, "pytest_adapter.py")):
                self.assertIsNone(pa.plugin_dir())


if __name__ == "__main__":
    unittest.main()
