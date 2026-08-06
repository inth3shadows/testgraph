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
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
