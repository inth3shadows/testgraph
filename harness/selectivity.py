"""S1 selectivity harness — how many of a registry's journeys `testgraph
select` picks per real commit, for a target with no hand-labeled oracle.

This is `accuracy.py`'s sibling, not its replacement: accuracy.py scores
recall/precision against hand labels on honeyslate. signedintake has no such
labels (TECHNICAL.md says so explicitly), so this harness reports SIZES —
how many journeys a commit selects out of the registry's total, and whether
`recall_degraded` fired — never precision or recall.

Faithfully simulates "testgraph running at commit C", the same way
accuracy.py does: checks C out into its own git worktree and builds a FRESH
codegraph index there, so index line numbers and `files.content_hash` both
agree with what's actually on disk at C. This is not optional bookkeeping —
it is the difference between a real answer and a wrong one. The original
signedintake sweep (#41) read every historical commit's diff against ONE
index built at the registry's tip commit; TECHNICAL.md flagged that as a
methodology limit even before it mattered ("sound for coverage, not line
alignment"). #52 turned the limit into a hard failure: `select` now hashes
every changed file against the index's recorded `content_hash` and treats a
mismatch as unmappable (`recall_degraded: true`, every journey listed). A
shared tip-commit index disagrees with almost every older commit's bytes, so
a naive re-run of the old approach wouldn't measure selectivity — it would
measure #52's guard firing on every row. A fresh per-commit index (this
file) is checked out and indexed at exactly the commit being asked about, so
its content hashes agree with itself by construction and content-drift
degrades stay a real signal instead of a sweep-wide false alarm.

Usage: python3 harness/selectivity.py [--bare PATH] [--registry PATH]
                                       [-n N] [--branch NAME]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from harness import _worktree as wtlib  # noqa: E402
from testgraph import select as sel  # noqa: E402

# Defaults name the target this harness was built for; --bare/--registry
# override for a different repo without touching the source.
BARE = "/home/ericm/personal_projects/signedintake/.bare"
REGISTRY = os.path.join(ROOT, "journeys", "signedintake.json")
CODEGRAPH = os.path.expanduser("~/.local/bin/codegraph")
DEFAULT_N = 14  # the cadence of the original 8-journey sweep (#41)


def last_n_commits(bare, n, branch):
    """The last N commits reachable from `branch`, newest first — merges
    included. #41's sweep counted a merge as one of its 14 rows (the 8-file
    merge that selected all 8 journeys), so excluding merges here would
    silently change what is being measured."""
    out = wtlib.run(["git", "-C", bare, "log", "--format=%H", "-n", str(n), branch])
    return out.stdout.split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bare", default=BARE)
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("-n", type=int, default=DEFAULT_N)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    registry = json.load(open(args.registry))
    total = len(registry["journeys"])

    shas = last_n_commits(args.bare, args.n, args.branch)
    tmp = tempfile.mkdtemp(prefix="tg-sel-")
    rows = []
    try:
        for sha in shas:
            wt = os.path.join(tmp, sha)
            try:
                db = wtlib.add_and_index(args.bare, sha, wt, CODEGRAPH)
            except wtlib.WorktreeError as e:
                rows.append((sha, e.stage, e.detail[:80]))
                if e.stage == "INDEX-FAIL":
                    wtlib.remove(args.bare, wt)
                continue
            try:
                # strict_registry=False: analysing history, not live use — a
                # journey that did not exist yet at an old commit is expected
                # (see accuracy.py's identical rationale), not registry rot.
                result = sel.select(wt, f"{sha}~1", sha, db, args.registry,
                                    strict_registry=False)
            except subprocess.CalledProcessError as e:
                # `{sha}~1` has no parent — only reachable if -n walks past
                # the repo's root commit.
                rows.append((sha, "NO-PARENT", str(e)[:80]))
                wtlib.remove(args.bare, wt)
                continue
            if result["status"] != "OK":
                rows.append((sha, "BLOCKED",
                             "; ".join(result.get("blocking", []))[:80]))
                wtlib.remove(args.bare, wt)
                continue
            selected = sorted(j["id"] for j in result["journeys"])
            desc = wtlib.run(["git", "-C", args.bare, "log", "-1", "--format=%s",
                              sha]).stdout.strip()
            rows.append((sha, desc[:50], len(selected), total,
                         result["recall_degraded"],
                         len(result["unresolved_journeys"]), selected))
            wtlib.remove(args.bare, wt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== testgraph selectivity — {os.path.basename(args.registry)} "
          f"({total} journeys), {len(shas)}-commit sweep, per-commit index ===")
    scored = [r for r in rows if len(r) == 7]
    for r in rows:
        if len(r) == 3:
            print(f"  {r[0][:10]}  {r[1]}: {r[2]}")
            continue
        sha, desc, n_sel, tot, degraded, unresolved, selected = r
        flag = "  RECALL_DEGRADED" if degraded else ""
        unres = f"  ({unresolved} journey(s) not yet in this commit's index)" if unresolved else ""
        print(f"\n  {sha[:10]}  {desc}")
        print(f"    selected : {n_sel}/{tot}  {selected}{flag}{unres}")

    print("\n--- summary ---")
    if not scored:
        print("  no scoreable commits (all failed — see rows above)")
        return 1
    counts = [r[2] for r in scored]
    degrades = sum(1 for r in scored if r[4])
    mean_sel = sum(counts) / len(counts)
    avoided_pct = 100 * (1 - mean_sel / total)
    le2 = sum(1 for c in counts if c <= 2)
    all_n = sum(1 for c in counts if c == total)
    most = sum(1 for c in counts if c >= total - 1)  # all, or all-but-one
    print(f"  commits scored        : {len(scored)} / {len(shas)}")
    print(f"  mean selected          : {mean_sel:.2f} / {total} "
          f"({100 * mean_sel / total:.1f}%)")
    print(f"  journey-runs avoided   : {avoided_pct:.1f}%")
    print(f"  <= 2 journeys          : {le2} of {len(scored)}")
    print(f"  all/most (>= {total - 1})     : {most} of {len(scored)}"
          f"  (all {total}: {all_n})")
    print(f"  recall_degraded fired  : {degrades} of {len(scored)}")
    # Full histogram — nothing bucketed away, so a reader can re-derive any
    # threshold the two headline buckets above don't happen to answer.
    hist = {}
    for c in counts:
        hist[c] = hist.get(c, 0) + 1
    print(f"  histogram (n_selected: n_commits): "
          f"{dict(sorted(hist.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
