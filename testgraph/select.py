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


def _is_test(path):
    base = path.rsplit("/", 1)[-1]
    return (
        "/tests/" in path
        or "/e2e/" in path
        or base.startswith("test_")
        or base.endswith("_test.py")
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
            if p.endswith(".py") and not _is_test(p):
                whole_files[p] = "renamed from"
        elif line.startswith("rename to "):
            p = line[len("rename to "):]
            if p.endswith(".py") and not _is_test(p):
                whole_files[p] = "renamed to"
        elif line.startswith("+++ ") and (
            line[4:].startswith("b/") or line[4:] == "/dev/null"
        ):
            path = line[4:]
            if path == "/dev/null":
                # whole-file deletion: the surviving path is on the '---' line
                if prev and prev.endswith(".py") and not _is_test(prev):
                    whole_files[prev] = "deleted"
                cur = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            cur = path if path.endswith(".py") and not _is_test(path) else None
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

    result = {"base": base, "head": head, "warnings": warnings,
              "unresolved_journeys": unresolved}
    if blocking:
        result["status"] = "BLOCKED"
        result["blocking"] = blocking
        return result

    ranges, whole_files = changed_ranges(repo, base, head)
    seeds = set()
    for f, rs in ranges.items():
        for lo, hi in rs:
            seeds.update(dbmod.nodes_for_lines(conn, f, lo, hi))

    # Whole-file changes (deletions, renames) have no line ranges to map: seed
    # every symbol the file contains. A file deleted in `head` is usually absent
    # from an index built at `head`, so this resolves only when the index still
    # predates the deletion (e.g. the per-commit harness). When it does not
    # resolve, impact is UNBOUNDED — we cannot know what depended on it — and
    # recall-first means saying so loudly rather than returning a narrow answer.
    unmapped = []
    for path, reason in whole_files.items():
        nodes = dbmod.nodes_in_file(conn, path)
        if nodes:
            seeds.update(nodes)
        else:
            unmapped.append(f"{path} ({reason})")

    impacted = dbmod.impacted_closure(conn, seeds)
    entry_map = reg.resolve_entries(conn, registry)

    touched = {}
    for nid in impacted.keys() & set(entry_map):
        touched.setdefault(entry_map[nid], set()).add(nid)

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
            f"{len(unmapped)} whole-file change(s) not in the index "
            f"({', '.join(unmapped)}) — impact is unbounded; all journeys listed"
        )
        selected = {j["id"] for j in journeys}
        for jid in registry.get("journeys", {}):
            if jid not in selected:
                journeys.append(
                    {
                        "id": jid,
                        "name": reg.journey_name(registry, jid),
                        "entries_hit": 0,
                        "rank": 0,
                        "confidence": 0.0,
                        "verify_manually": True,
                        "reason": "unmappable whole-file change",
                    }
                )

    journeys.sort(key=lambda j: (-j["rank"], j["id"]))

    result.update(
        status="OK",
        changed_files=sorted(ranges),
        whole_file_changes=whole_files,
        recall_degraded=bool(unmapped),
        seed_symbols=len(seeds),
        impacted_symbols=len(impacted),
        journeys=journeys,
    )
    return result


def _render(result):
    lines = [f"base..head: {result['base']}..{result['head']}"]
    for w in result.get("warnings", []):
        lines.append(f"  WARN: {w}")
    if result["status"] == "BLOCKED":
        lines.append("STATUS: BLOCKED — index not trustworthy")
        for b in result["blocking"]:
            lines.append(f"  x {b}")
        return "\n".join(lines)
    lines.append(
        f"changed .py: {len(result['changed_files'])} | "
        f"seeds: {result['seed_symbols']} | impacted symbols: {result['impacted_symbols']}"
    )
    for path, reason in sorted(result.get("whole_file_changes", {}).items()):
        lines.append(f"  whole-file: {path} ({reason})")
    if result.get("recall_degraded"):
        lines.append("  RECALL DEGRADED — unbounded impact, all journeys listed")
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
        default=os.path.join(os.path.dirname(__file__), "..", "journeys", "honeyslate.json"),
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    result = select(args.repo, args.base, args.head, db_path, args.registry)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_render(result))
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
