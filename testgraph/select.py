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
    """{file: [(lo, hi), ...]} of changed line ranges in product .py files.

    Split out from git invocation so it is unit-testable. A '+++ ' line is only
    treated as a file header when it carries a 'b/' or '/dev/null' path, so a
    changed content line that renders as '+++ ...' is not misread as a header.
    """
    ranges, cur = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ ") and (
            line[4:].startswith("b/") or line[4:] == "/dev/null"
        ):
            path = line[4:]
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
    return {f: r for f, r in ranges.items() if r}


def changed_ranges(repo, base, head):
    diff = subprocess.run(
        ["git", "-C", repo, "diff", "--unified=0", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return _parse_unified_diff(diff)


def select(repo, base, head, db_path, registry_path):
    conn = dbmod.connect(db_path)
    registry = reg.load(registry_path)

    blocking, warnings = integrity.check(conn, repo, registry.get("spot_checks", {}))
    result = {"base": base, "head": head, "warnings": warnings}
    if blocking:
        result["status"] = "BLOCKED"
        result["blocking"] = blocking
        return result

    ranges = changed_ranges(repo, base, head)
    seeds = set()
    for f, rs in ranges.items():
        for lo, hi in rs:
            seeds.update(dbmod.nodes_for_lines(conn, f, lo, hi))

    impacted = dbmod.impacted_closure(conn, seeds)
    entry_map = reg.resolve_entries(conn, registry)

    touched = {}
    for nid in impacted & set(entry_map):
        touched.setdefault(entry_map[nid], set()).add(nid)

    journeys = []
    for jid, ents in touched.items():
        fanin = sum(dbmod.caller_edge_count(conn, e) for e in ents)
        journeys.append(
            {
                "id": jid,
                "name": reg.journey_name(registry, jid),
                "entries_hit": len(ents),
                "rank": fanin,
            }
        )
    journeys.sort(key=lambda j: (-j["rank"], j["id"]))

    result.update(
        status="OK",
        changed_files=sorted(ranges),
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
    if not result["journeys"]:
        lines.append("journeys to test: NONE (no product-behavior change detected)")
    else:
        lines.append(f"journeys to test ({len(result['journeys'])}), ranked:")
        for j in result["journeys"]:
            lines.append(f"  [{j['rank']:>3}] {j['id']}  {j['name']}  ({j['entries_hit']} entry)")
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
