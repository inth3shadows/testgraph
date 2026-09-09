"""Executable checks over the SHIPPED registries in `journeys/*.json`.

Every registry in that directory is load-bearing product data, and until now
nothing verified any of them. That gap is not hypothetical — it is the exact
shape of the defect closed in `ae128bb` (finding 8): a trailing comma in an
approved registry makes `json.load` raise, `reg.resolve_for_repo` swallows the
`ValueError` and returns None, and the repo is then reported as having *no*
registry. The pre-push hook logs NO_REGISTRY and exits 0, `record` tells the
user to draft a file they already have, and nothing anywhere fails. A typo
silently disables the tool for that repo until somebody notices it stopped
talking.

These tests make that falsifiable at commit time. They are deliberately
INDEX-FREE: honeyslate's and signedintake's codegraph databases are not present
in every checkout (and never in CI), so asserting that their entries resolve
would be a test that fails for reasons having nothing to do with the registry.
Symbol resolution is already covered where an index exists — `unresolved()` and
`unchecked_entries()` are called by `select` on every real run.

What each check pins:

  * parses at all — the `ae128bb` finding 8 defect, caught before push
  * `target` present and UNIQUE across files — `resolve_for_repo` scans the
    directory in sorted filename order and returns the FIRST match, so two files
    claiming one target means one of them silently never loads, chosen by
    alphabetical accident
  * `approved` present and boolean — `approval_warning` treats a missing key as
    unknown provenance, which is correct behaviour but a poor thing to discover
    from a warning on somebody's push
  * journeys non-empty, each with a name and at least one entry — a journey with
    no entries can never be selected and disappears from every answer while the
    exported map still advertises it (the `unresolved()` rot problem, but
    structural rather than drift-induced)
  * ids sort — `journey_sort_key` is used to order every rendered answer
  * for testgraph's OWN registry only, that each entry file exists on disk. This
    is the one target whose source is in this repo, so it is the one place a
    rename can be caught here rather than at the next push.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from testgraph import registry as reg  # noqa: E402

JOURNEYS_DIR = os.path.join(ROOT_DIR, "journeys")


def registry_files():
    return sorted(
        os.path.join(JOURNEYS_DIR, f)
        for f in os.listdir(JOURNEYS_DIR)
        if f.endswith(".json")
    )


class ShippedRegistries(unittest.TestCase):
    def test_directory_is_not_empty(self):
        """A passing suite over zero files would be a vacuous green."""
        self.assertTrue(registry_files(), "no registries found in journeys/")

    def test_each_parses_and_is_well_formed(self):
        for path in registry_files():
            with self.subTest(registry=os.path.basename(path)):
                try:
                    data = reg.load(path)
                except ValueError as exc:
                    self.fail(
                        f"{path} does not parse ({exc}) — this silently disables "
                        f"the pre-push hook for its target rather than failing"
                    )

                self.assertIsInstance(data, dict)
                target = data.get("target")
                self.assertIsInstance(target, str)
                self.assertTrue(target.strip(), f"{path} has an empty target")

                self.assertIn(
                    "approved", data, f"{path} has no `approved` marker"
                )
                self.assertIsInstance(data["approved"], bool)

                journeys = data.get("journeys")
                self.assertIsInstance(journeys, dict, f"{path} has no journeys object")
                self.assertTrue(journeys, f"{path} declares no journeys")

                for jid, journey in journeys.items():
                    self.assertIsInstance(journey, dict, f"{path}:{jid}")
                    name = journey.get("name")
                    self.assertIsInstance(name, str, f"{path}:{jid} has no name")
                    self.assertTrue(name.strip(), f"{path}:{jid} has an empty name")

                    entries = journey.get("entries")
                    self.assertIsInstance(entries, list, f"{path}:{jid} entries")
                    self.assertTrue(
                        entries,
                        f"{path}:{jid} has no entries — it can never be selected, "
                        f"yet the exported map still advertises it",
                    )
                    for entry in entries:
                        self.assertIsInstance(entry, dict, f"{path}:{jid}")
                        self.assertTrue(
                            (entry.get("name") or "").strip(),
                            f"{path}:{jid} has an entry with no symbol name",
                        )
                        self.assertTrue(
                            (entry.get("file") or "").strip(),
                            f"{path}:{jid} entry {entry.get('name')!r} has no file; "
                            f"resolve_symbol would match that name in ANY file",
                        )

                # `journey_sort_key` orders every rendered answer. Merely CALLING
                # it proved nothing — it cannot raise for any `str` key, so the
                # guard stayed green under any mutation. Assert the property the
                # renderer actually depends on: J2 before J10, i.e. numeric and
                # not lexicographic.
                ordered = sorted(journeys, key=reg.journey_sort_key)
                numbered = [j for j in ordered if j[1:].isdigit() and j[0] == "J"]
                self.assertEqual(
                    numbered,
                    sorted(numbered, key=lambda j: int(j[1:])),
                    f"{path} journey ids do not sort numerically",
                )

    def test_targets_are_unique(self):
        """`resolve_for_repo` returns the FIRST file whose target matches, in
        sorted filename order. A duplicate target means one registry silently
        never loads and which one is decided alphabetically."""
        seen = {}
        for path in registry_files():
            target = reg.load(path).get("target")
            self.assertNotIn(
                target,
                seen,
                f"{path} and {seen.get(target)} both claim target {target!r}",
            )
            seen[target] = path

    def test_testgraph_entry_files_exist(self):
        """testgraph's registry is the only one whose target lives in this repo,
        so it is the only one where a rename can be caught at commit time rather
        than by a degraded answer on the next push."""
        path = os.path.join(JOURNEYS_DIR, "testgraph.json")
        self.assertTrue(os.path.exists(path), "testgraph has no registry")
        data = reg.load(path)
        self.assertEqual(data["target"], "testgraph")
        for jid, journey in data["journeys"].items():
            for entry in journey["entries"]:
                full = os.path.join(ROOT_DIR, entry["file"])
                self.assertTrue(
                    os.path.exists(full),
                    f"{jid} entry {entry['name']!r} names {entry['file']}, "
                    f"which does not exist",
                )

    def test_testgraph_registry_is_resolvable_by_its_target(self):
        """The end-to-end path the hook depends on: `resolve_for_repo` must find
        testgraph's registry by target and no other.

        The repo path is SYNTHESIZED rather than taken from `ROOT_DIR`.
        `repo_name` derives the target from the checkout's directory name (or its
        bare-worktree parent), so asserting on `ROOT_DIR` made this test pass or
        fail on what the directory happens to be CALLED. It failed in any
        worktree not literally named `testgraph` — including every scratch
        worktree `harness/selectivity.py` and `harness/accuracy.py` create, which
        is precisely where it will run once testgraph is its own measured
        target."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "testgraph", "main")
            os.makedirs(repo)
            os.makedirs(os.path.join(tmp, "testgraph", ".bare"))
            self.assertEqual(reg.repo_name(repo), "testgraph")
            path = reg.resolve_for_repo(repo, journeys_dir=JOURNEYS_DIR)

        self.assertIsNotNone(path, "resolve_for_repo found no registry for testgraph")
        self.assertEqual(os.path.basename(path), "testgraph.json")
        self.assertIsNone(
            reg.approval_warning(reg.load(path)),
            "testgraph's own registry is shipped unapproved",
        )

    def test_the_registry_carries_spot_checks_for_the_integrity_guard(self):
        """`select` hands `registry["spot_checks"]` to `integrity.check`. An
        absent key yields `{}`, which silently no-ops the CALLER-COUNT check —
        the only one of the three that catches the 2026-07-17 incident
        `integrity.py` exists for, and the one `codegraph sync` cannot clear.
        Both other shipped registries carry spot-checks and `propose` emits them
        automatically; a hand-authored registry is the one way to omit them."""
        for path in registry_files():
            with self.subTest(registry=os.path.basename(path)):
                spots = reg.load(path).get("spot_checks")
                self.assertIsInstance(spots, dict, f"{path} has no spot_checks")
                self.assertTrue(spots, f"{path} has an empty spot_checks")
                for name, spec in spots.items():
                    self.assertIsInstance(spec, dict, f"{path}:{name}")
                    self.assertIsInstance(
                        spec.get("min_caller_edges"), int, f"{path}:{name}"
                    )
                    self.assertTrue(
                        (spec.get("file") or "").strip(),
                        f"{path}:{name} has no file, so the symbol is matched "
                        f"by bare name in any file",
                    )


class RegistryDiscoveryTests(unittest.TestCase):
    """Where `resolve_for_repo` looks, and why the old answer was unusable.

    The search directory used to be package-relative and NOTHING else. That works
    in a source checkout and is silently useless anywhere else: the wheel ships
    `testgraph/` alone, so on `pip install testgraph` the path resolves to
    `site-packages/journeys`, which does not exist. Every repo answered "no
    journey registry found" forever — including through the MCP server, which
    would have returned that to every agent that ever called it. Measured on a
    real wheel install, not inferred, which is why these tests exist at all.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # `repo_name` reads the `.bare/` marker beside the worktree.
        self.repo = os.path.join(self.tmp, "widget", "main")
        os.makedirs(self.repo)
        os.makedirs(os.path.join(self.tmp, "widget", ".bare"))
        self.addCleanup(os.environ.pop, reg.ENV_JOURNEYS_DIR, None)
        os.environ.pop(reg.ENV_JOURNEYS_DIR, None)

    def _write(self, directory, fname, target):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, fname)
        with open(path, "w") as fh:
            json.dump({"target": target, "journeys": {}}, fh)
        return path

    def _repo_dir(self):
        return os.path.join(self.repo, reg.REPO_JOURNEYS_SUBDIR)

    def test_a_registry_in_the_repo_is_found(self):
        """The case a pip-installed user is in, and could not reach before."""
        want = self._write(self._repo_dir(), "widget.json", "widget")
        self.assertEqual(want, reg.resolve_for_repo(self.repo))

    def test_the_repo_wins_over_the_package_directory(self):
        """Most specific first.

        A checkout of testgraph itself ships three dogfood registries; a consumer
        repo that carries its own must not be overridden by one of those merely
        because the package directory happens to be importable.
        """
        pkg = os.path.join(self.tmp, "pkgjourneys")
        self._write(pkg, "widget.json", "widget")
        want = self._write(self._repo_dir(), "widget.json", "widget")
        with unittest.mock.patch.object(reg, "PACKAGE_JOURNEYS_DIR", pkg):
            self.assertEqual(want, reg.resolve_for_repo(self.repo))

    def test_the_env_var_wins_over_the_repo(self):
        """The escape hatch outranks both — a registry under review elsewhere."""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        want = self._write(elsewhere, "widget.json", "widget")
        self._write(self._repo_dir(), "widget.json", "widget")
        os.environ[reg.ENV_JOURNEYS_DIR] = elsewhere
        self.assertEqual(want, reg.resolve_for_repo(self.repo))

    def test_an_explicit_directory_searches_that_one_and_no_other(self):
        """An explicit path is an instruction, not a hint.

        The harness passes one when analysing history; falling through to a
        repo-local registry would quietly score a different file than the caller
        named.
        """
        self._write(self._repo_dir(), "widget.json", "widget")
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertIsNone(reg.resolve_for_repo(self.repo, journeys_dir=empty))

    def test_a_missing_directory_is_skipped_not_fatal(self):
        """The package directory is absent in every wheel install."""
        want = self._write(self._repo_dir(), "widget.json", "widget")
        with unittest.mock.patch.object(
            reg, "PACKAGE_JOURNEYS_DIR", os.path.join(self.tmp, "nope")
        ):
            self.assertEqual(want, reg.resolve_for_repo(self.repo))

    def test_a_mismatched_target_in_the_repo_is_still_refused(self):
        """Being repo-local does not make a copied registry correct.

        A registry copied from another project and left unedited is exactly the
        case that used to report the resulting disagreement as a stale index.
        Returning None is the safe answer; the target is the claim, not the path.
        """
        self._write(self._repo_dir(), "widget.json", "someone-else")
        self.assertIsNone(reg.resolve_for_repo(self.repo))

    def test_the_not_found_message_names_every_directory_searched(self):
        """'No registry found' without a WHERE is unactionable.

        The reader cannot otherwise tell a missing file from a target typo from a
        directory the installed package can never see.
        """
        looked = reg.where_it_looked(self.repo)
        self.assertIn(os.path.normpath(self._repo_dir()), looked)
        self.assertIn("journeys", looked)

    def test_the_shipped_registries_are_still_found_from_this_checkout(self):
        """The package-relative fallback is a fallback, not a removal."""
        repo = os.path.join(self.tmp, "testgraph", "main")
        os.makedirs(repo)
        os.makedirs(os.path.join(self.tmp, "testgraph", ".bare"))
        path = reg.resolve_for_repo(repo)
        self.assertIsNotNone(path, "the checkout's own registries stopped resolving")
        self.assertEqual(os.path.basename(path), "testgraph.json")


if __name__ == "__main__":
    unittest.main()
