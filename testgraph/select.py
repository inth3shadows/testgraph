"""testgraph select — git diff -> ranked affected journeys.

Recall-first: the closure over-selects on shared-symbol changes rather than
risk dropping a truly-affected journey. Test/e2e file changes are ignored as
seeds (they don't change product behavior).

Usage:
    python -m testgraph.select --repo <path> [--base SHA] [--head SHA]
                               [--db PATH] [--registry PATH] [--json]
"""
import argparse
import json
import os
import re
import subprocess
import sys

from . import db as dbmod
from . import integrity
from . import registry as reg

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# Extensions CodeGraph indexes for this stack. Previously only `.py` was
# considered, so a change to any frontend file produced zero seeds and select
# answered "journeys to test: NONE" -- while `export`'s map, which walks all
# indexed nodes, listed those same files against J8. Two tools, different
# answers, and the map was the correct one (issue #21).
#
# Widening is not free, and the earlier claim that it "cannot invent impact" was
# only true for extensions the indexer does not cover (no nodes -> no seeds).
# Where the indexer DOES cover the extension it can add a false positive:
# measured mean precision fell 0.84 -> 0.68 when the frontend was seeded, via a
# 0.5-confidence edge to J8 (TECHNICAL.md). That path is flagged
# `verify_manually`, which is the intended trade -- recall stayed at 1.00.
PRODUCT_EXT = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
               ".cts", ".svelte", ".vue")

# Type declarations carry no runtime behavior and have no nodes, so seeding them
# cannot help; after #29 an extension-accepted file with no nodes degrades the
# whole answer to "test everything", so admitting them would be actively worse.
DECLARATION_EXT = (".d.ts", ".d.mts", ".d.cts")

# Directory names that mean "this is test code", matched as whole path segments.
# Substring matching on "/tests/" missed a repo-root `tests/` or `__tests__/`
# directory entirely, so root-level test files were seeded as product code.
TEST_DIRS = frozenset({"tests", "e2e", "__tests__"})


def _is_product(path):
    return (
        path.endswith(PRODUCT_EXT)
        and not path.endswith(DECLARATION_EXT)
        and not _is_test(path)
    )


def _is_test(path):
    parts = path.split("/")
    base = parts[-1]
    return (
        bool(TEST_DIRS.intersection(parts[:-1]))
        or base.startswith("test_")
        or base.endswith("_test.py")
        # JS/TS conventions, now that non-Python paths are seeded
        or ".test." in base
        or ".spec." in base
    )


def _parse_unified_diff(diff):
    """Parse a `git diff --unified=0` into
    `(ranges, whole_files)` where

      ranges      = {file: [(lo, hi), ...]}  changed line ranges, and
      whole_files = {path: reason}           files with no usable line ranges

    `whole_files` covers deletions and renames. Both used to vanish silently: a
    deleted file's '+++ ' header is '/dev/null', which is not a .py path, so the
    file was dropped and its dependents were never seeded — silent
    under-selection, the unsafe direction. A rename with no content edit
    produces no '@@' hunks at all, yet changes the module path for every
    importer.

    A '+++ ' line is only treated as a file header when it carries a 'b/' or
    '/dev/null' path, so a changed content line that renders as '+++ ...' is not
    misread as a header.
    """
    ranges, whole_files, cur, prev = {}, {}, None, None
    for line in diff.splitlines():
        if line.startswith("--- "):
            p = line[4:]
            prev = p[2:] if p.startswith("a/") else None
        elif line.startswith("rename from "):
            p = line[len("rename from "):]
            if _is_product(p):
                whole_files[p] = "renamed from"
        elif line.startswith("rename to "):
            p = line[len("rename to "):]
            if _is_product(p):
                whole_files[p] = "renamed to"
        elif line.startswith("+++ ") and (
            line[4:].startswith("b/") or line[4:] == "/dev/null"
        ):
            path = line[4:]
            if path == "/dev/null":
                # whole-file deletion: the surviving path is on the '---' line
                if prev and _is_product(prev):
                    whole_files[prev] = "deleted"
                cur = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            cur = path if _is_product(path) else None
            if cur is not None:
                ranges.setdefault(cur, [])
        elif cur is not None and line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                start = int(m.group(1))
                cnt = int(m.group(2) or 1)
                if cnt > 0:
                    ranges[cur].append((start, start + cnt - 1))
                else:
                    # Pure deletion (+N,0): no new lines, but behavior changed.
                    # Seed the enclosing node at the deletion boundary so the
                    # affected journey is still selected (recall-first).
                    lo = max(1, start)
                    ranges[cur].append((lo, lo + 1))
    return {f: r for f, r in ranges.items() if r}, whole_files


def changed_ranges(repo, base, head):
    diff = subprocess.run(
        # -M: detect renames so a moved module is seeded whole rather than read
        # as an unrelated delete + add.
        ["git", "-C", repo, "diff", "--unified=0", "-M", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return _parse_unified_diff(diff)


def select(repo, base, head, db_path, registry_path, strict_registry=True):
    conn = dbmod.connect(db_path)
    registry = reg.load(registry_path)

    blocking, warnings = integrity.check(
        conn,
        repo,
        registry.get("spot_checks", {}),
        schema_pin=registry.get("codegraph_schema_version"),
    )
    # Provenance of the registry itself. Everything below checks whether the
    # registry AGREES with the code; this checks whether anyone ever read it. A
    # machine-drafted registry (issue #6) is runnable on purpose, so this is a
    # warning, not a block -- but a `NONE` answer computed from an unreviewed
    # registry means "not registered", not "not affected".
    approval = reg.approval_warning(registry)
    if approval:
        warnings.append(approval)

    # A journey whose entries do not resolve can never be selected -- silent
    # under-selection. Blocking is right for live use. But when ANALYSING HISTORY
    # (the accuracy harness checks out old commits) a journey that simply did not
    # exist yet is expected, not rot, and blocking would just shrink the scored
    # set. `strict_registry=False` downgrades it to a reported field so the
    # distinction is explicit rather than accidental.
    unresolved = [jid for jid, _ in reg.unresolved(conn, registry)]
    if unresolved:
        detail = ", ".join(
            f"{jid} ({reg.journey_name(registry, jid)})" for jid in unresolved
        )
        if strict_registry:
            blocking.append(
                f"journeys with no resolvable entry symbol: {detail} — registry is "
                f"stale against the index; they can never be selected"
            )
        else:
            warnings.append(
                f"journeys absent from this index (not scored): {detail}"
            )

    # Drift the index cannot see: the registry and the index can agree while both
    # are stale against the source. A live parse is the only check in this pipeline
    # that reads the working tree, so it is the only one that catches a handler
    # renamed since the last `codegraph index` (issue #7). Reported as a
    # first-class field AND a warning, never blocking — see registry.live_drift.
    drift = reg.live_drift(repo, registry)
    for jid, name, rel, why in drift:
        # Remedy per reason, not one hard-coded "re-index". live_drift reads the
        # WORKING TREE while everything else here reads committed history, so an
        # uncommitted rename — the common case — would otherwise send an agent off
        # to rebuild an index that cannot change this answer.
        warnings.append(
            f"journey {jid} entry `{name}` ({rel}): {why} — {reg.remedy_for(why)}"
        )
    # Entries no parser covers are NOT drift and must not ride the warning channel:
    # re-indexing can never clear them, so a permanent warning would train the
    # reader to ignore warnings. Reported as their own field instead.
    unchecked = reg.unchecked_entries(registry)

    result = {"base": base, "head": head, "warnings": warnings,
              "unresolved_journeys": unresolved,
              "entry_drift": [
                  {"journey": jid, "entry": name, "file": rel, "reason": why}
                  for jid, name, rel, why in drift
              ],
              "entries_unchecked": [
                  {"journey": jid, "entry": name, "file": rel}
                  for jid, name, rel in unchecked
              ]}
    if blocking:
        result["status"] = "BLOCKED"
        result["blocking"] = blocking
        return result

    ranges, whole_files = changed_ranges(repo, base, head)

    # A changed file whose hunks resolve to NO node is the same failure as an
    # unmappable whole-file change, and it used to pass silently: seeds stayed
    # empty, no warning was raised, and the answer was a confident
    # "journeys to test: NONE" (issue #29). It happens whenever the index
    # predates the file (a newly added module) or covers the extension in
    # PRODUCT_EXT but not in this repo's graph. Per-file node sets, not one
    # running total, so one mapped file cannot mask an unmapped one.
    unmapped = []
    unmapped_files = set()
    seeds = set()
    seeds_by_file = {}
    for f, rs in sorted(ranges.items()):
        in_file = set()
        for lo, hi in rs:
            in_file.update(dbmod.nodes_for_lines(conn, f, lo, hi))
        if in_file:
            seeds.update(in_file)
            seeds_by_file[f] = in_file
        else:
            unmapped.append(f"{f} (changed lines map to no indexed symbol)")
            unmapped_files.add(f)

    # Whole-file changes (deletions, renames) have no line ranges to map: seed
    # every symbol the file contains. A file deleted in `head` is usually absent
    # from an index built at `head`, so this resolves only when the index still
    # predates the deletion (e.g. the per-commit harness). When it does not
    # resolve, impact is UNBOUNDED — we cannot know what depended on it — and
    # recall-first means saying so loudly rather than returning a narrow answer.
    for path, reason in whole_files.items():
        nodes = dbmod.nodes_in_file(conn, path)
        if nodes:
            seeds.update(nodes)
            # A rename that ALSO carries edited hunks lands in both `ranges`
            # and `whole_files` for the new path. `seeds` above always gets
            # the full file (recall-first: a rename changes the module path
            # for every importer, so the whole file is in play regardless of
            # which lines moved) — but for the confinement check specifically,
            # letting the broader whole-file set clobber a precise range-based
            # entry can mask a real issue-#63 confinement in the lines that
            # actually changed behind unrelated untouched symbols that happen
            # to reach elsewhere. Keep the narrower, already-set entry.
            if path not in seeds_by_file:
                seeds_by_file[path] = set(nodes)
        else:
            unmapped.append(f"{path} ({reason})")
            unmapped_files.add(path)

    # A changed file that is NEWER than its index row is a third way to be
    # unmappable, and the quietest. The other two resolve to no node and are
    # obvious; this one resolves to the WRONG node. Seeds come from line ranges
    # (`nodes_for_lines`), so a file that gained twenty lines above a function
    # since it was indexed hands the diff's line numbers to whatever symbol used
    # to live there — a neighbouring function, or nothing. The answer stays
    # confident and can be narrower than the truth, which is the one failure
    # this selector is built to refuse.
    #
    # The seeds computed above are KEPT, not discarded: whatever they resolved to
    # is still evidence, and recall-first means adding doubt, not removing rows.
    # Joining `unmapped` degrades the answer to "every journey, verify manually"
    # — the same treatment issue #29 established for a file with no symbols.
    #
    # The `pre-push` hook runs `codegraph sync` first precisely so this stays the
    # exceptional path rather than every push.
    drifted = integrity.content_drift(conn, repo, set(ranges) | set(whole_files))
    for path in sorted(drifted):
        unmapped.append(f"{path} (bytes differ from the indexed copy — line spans are stale)")
        unmapped_files.add(path)

    impacted = dbmod.impacted_closure(conn, seeds)

    # A closure that resolves fine but never leaves the file(s) its seeds
    # started in is a second, silent way to be blind (issue #63) — distinct
    # from `unmapped` above (no node at all). The seeds have symbols; those
    # symbols just have no recorded outbound reach. Checked per file, not
    # against the union of every seeded file in this diff, so a genuinely
    # cross-file change cannot mask a same-diff file that stayed confined.
    # Files already in `unmapped` are skipped: their seeds are untrusted, not
    # evidence of confinement.
    #
    # One recursive traversal per seeded file (beyond the single-file reuse
    # below): a mechanical rename/refactor touching many files pays for many
    # extra closures. Accepted for the same reason `journeys` already loops
    # `caller_edge_count` per entry above — this selector is recall-first and
    # already spends per-item DB round trips elsewhere; a diff wide enough to
    # feel this is also wide enough to be a whole-file/`unmapped` case on the
    # commonest paths. Revisit if this ever shows up in profiling.
    confined_files = []
    for f, file_seeds in sorted(seeds_by_file.items()):
        if f in unmapped_files:
            continue
        # `impacted` is already this file's closure whenever its seeds equal
        # the full seed set — reuse it instead of re-running the same
        # recursive traversal. Checked by value, not by `len(seeds_by_file)
        # == 1`: inferring it from the file count silently breaks if a future
        # seed source populates `seeds` without also updating
        # `seeds_by_file`.
        file_impacted = (
            impacted if file_seeds == seeds else dbmod.impacted_closure(conn, file_seeds)
        )
        reached_files = dbmod.closure_files(conn, file_impacted.keys())
        if reached_files and reached_files <= {f}:
            confined_files.append(f)
    entry_map = reg.resolve_entries(conn, registry)

    touched = {}
    for nid in impacted.keys() & set(entry_map):
        for jid in entry_map[nid]:
            touched.setdefault(jid, set()).add(nid)

    journeys = []
    for jid, ents in touched.items():
        fanin = sum(dbmod.caller_edge_count(conn, e) for e in ents)
        # Strongest route into the journey: if ANY entry is reached confidently,
        # the selection is trustworthy.
        conf = max(impacted[e] for e in ents)
        journeys.append(
            {
                "id": jid,
                "name": reg.journey_name(registry, jid),
                "entries_hit": len(ents),
                "rank": fanin,
                "confidence": round(conf, 3),
                "verify_manually": conf <= dbmod.LOW_CONFIDENCE,
            }
        )
    # Unmappable whole-file change -> unbounded impact. Add every journey the
    # closure did not already select, flagged for manual verification, so the
    # answer degrades toward "test everything" instead of toward silence.
    if unmapped:
        warnings.append(
            f"{len(unmapped)} changed file(s) the index cannot be trusted for "
            f"({', '.join(unmapped)}) — impact is unbounded; all journeys listed"
        )
        selected = {j["id"] for j in journeys}
        for jid in sorted(registry.get("journeys", {}), key=reg.journey_sort_key):
            if jid not in selected:
                journeys.append(
                    {
                        "id": jid,
                        "name": reg.journey_name(registry, jid),
                        "entries_hit": 0,
                        "rank": 0,
                        "confidence": 0.0,
                        "verify_manually": True,
                        "reason": "change with no resolvable symbols",
                    }
                )

    journeys.sort(key=lambda j: (-j["rank"], reg.journey_sort_key(j["id"])))

    # One warning per file, not one combined message: a summed node count
    # across several confined files tells a reader nothing about which file
    # contributed how much, and the per-file NOTE lines in `_render` below
    # already report them separately -- the warnings channel should agree.
    for f in confined_files:
        warnings.append(
            f"impact for {f} did not leave the file it started in "
            f"({len(seeds_by_file[f])} node(s), all local) — either the module "
            f"is genuinely leaf-only, or its callers are not linked in the "
            f"index; that file's own contribution to this answer is UNKNOWN, "
            f"not verified-safe, even though other changed files may still be "
            f"selecting journeys above"
        )

    result.update(
        status="OK",
        changed_files=sorted(ranges),
        whole_file_changes=whole_files,
        recall_degraded=bool(unmapped),
        closure_confined=confined_files,
        seed_symbols=len(seeds),
        impacted_symbols=len(impacted),
        journeys=journeys,
    )
    return result


def _render(result):
    lines = [f"base..head: {result['base']}..{result['head']}"]
    for w in result.get("warnings", []):
        lines.append(f"  WARN: {w}")
    # visible without --json: a CLI user could otherwise not tell that some entry
    # symbols were never checked against source
    for u in result.get("entries_unchecked", []):
        lines.append(
            f"  NOTE: {u['journey']} entry `{u['entry']}` ({u['file']}) not "
            f"verified against source — no parser for that file type"
        )
    if result["status"] == "BLOCKED":
        lines.append("STATUS: BLOCKED — index not trustworthy")
        for b in result["blocking"]:
            lines.append(f"  x {b}")
        return "\n".join(lines)
    lines.append(
        f"changed files: {len(result['changed_files'])} | "
        f"seeds: {result['seed_symbols']} | impacted symbols: {result['impacted_symbols']}"
    )
    for path, reason in sorted(result.get("whole_file_changes", {}).items()):
        lines.append(f"  whole-file: {path} ({reason})")
    if result.get("recall_degraded"):
        lines.append("  RECALL DEGRADED — unbounded impact, all journeys listed")
    for f in result.get("closure_confined", []):
        lines.append(
            f"  NOTE: impact for {f} did not leave the file it started in — "
            f"that file's own contribution is UNKNOWN, not verified-safe"
        )
    if not result["journeys"]:
        lines.append("journeys to test: NONE (no product-behavior change detected)")
    else:
        lines.append(f"journeys to test ({len(result['journeys'])}), ranked:")
        for j in result["journeys"]:
            flag = "  ! VERIFY MANUALLY (weak edge path)" if j["verify_manually"] else ""
            lines.append(
                f"  [{j['rank']:>3}] {j['id']}  {j['name']}  "
                f"({j['entries_hit']} entry, conf {j['confidence']}){flag}"
            )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testgraph.select")
    ap.add_argument("--repo", default="/home/ericm/personal_projects/honeyslate/main")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--db", default=None, help="defaults to <repo>/.codegraph/codegraph.db")
    ap.add_argument(
        "--registry",
        default=None,
        help="defaults to the journeys/*.json whose `target` matches <repo>",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    # Resolve by the registry's own `target`, and REFUSE rather than fall back.
    # This used to default to honeyslate's registry unconditionally, so pointing
    # --repo at any other project loaded the wrong journeys and then reported the
    # resulting disagreement as a stale index — a confidently wrong diagnosis.
    registry_path = args.registry or reg.resolve_for_repo(args.repo)
    if registry_path is None:
        print(
            f"no journey registry found for repo `{reg.repo_name(args.repo)}` "
            f"({args.repo}) — add journeys/<name>.json with \"target\": "
            f"\"{reg.repo_name(args.repo)}\", draft one with `python3 -m "
            f"testgraph.propose --repo {args.repo}`, or pass --registry",
            file=sys.stderr,
        )
        return 2
    result = select(args.repo, args.base, args.head, db_path, registry_path)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_render(result))
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
