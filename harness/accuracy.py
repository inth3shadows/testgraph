"""S1 accuracy harness — recall-first validation of testgraph select.

Faithfully simulates "testgraph running at commit C": checks each commit out in
an isolated worktree, builds its OWN codegraph index there (so index line
numbers align with the diff), runs the selector for C~1..C, and scores against
the hand-labeled oracle.

Recall is the pass/fail metric (must be 1.0 on every commit — never drop a
truly-affected journey). Precision is reported, not gated.

Usage: python3 harness/accuracy.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from harness import _worktree  # noqa: E402
from testgraph import select as sel  # noqa: E402

BARE = "/home/ericm/personal_projects/honeyslate/.bare"
REGISTRY = os.path.join(ROOT, "journeys", "honeyslate.json")
CODEGRAPH = os.path.expanduser("~/.local/bin/codegraph")


def score(selected, oracle):
    sel_set, ora_set = set(selected), set(oracle)
    if not ora_set:
        # nothing should be selected; recall is vacuously satisfied
        return 1.0, (1.0 if not sel_set else 0.0), len(sel_set)
    hit = sel_set & ora_set
    recall = len(hit) / len(ora_set)
    precision = len(hit) / len(sel_set) if sel_set else 0.0
    return recall, precision, len(sel_set - ora_set)


def main():
    labels = json.load(open(os.path.join(HERE, "labels_honeyslate.json")))
    tmp = tempfile.mkdtemp(prefix="tg-acc-")
    rows, min_recall, precisions = [], 1.0, []
    try:
        for c in labels["commits"]:
            sha, oracle = c["sha"], c["oracle"]
            wtdir = os.path.join(tmp, sha)
            try:
                db = _worktree.add_and_index(BARE, sha, wtdir, CODEGRAPH)
            except _worktree.WorktreeError as e:
                rows.append((sha, e.stage, e.detail[:60]))
                if e.stage == "INDEX-FAIL":
                    _worktree.remove(BARE, wtdir)
                continue
            # Historical commits legitimately predate some journeys; that is not
            # registry rot, so do not block on it -- but do report it.
            result = sel.select(wtdir, f"{sha}~1", sha, db, REGISTRY,
                                strict_registry=False)
            if result["status"] != "OK":
                rows.append((sha, "BLOCKED", "; ".join(result.get("blocking", []))[:60]))
                _worktree.remove(BARE, wtdir)
                continue
            selected = [j["id"] for j in result["journeys"]]
            # A journey absent from this commit's index cannot be selected, so it
            # must not count against precision or recall either.
            absent = set(result.get("unresolved_journeys", []))
            if absent:
                oracle = [j for j in oracle if j not in absent]
            recall, precision, fp = score(selected, oracle)
            min_recall = min(min_recall, recall)
            if oracle:
                precisions.append(precision)
            rows.append((sha, c["desc"][:34], oracle, sorted(selected),
                         f"R={recall:.2f} P={precision:.2f} FP={fp}"))
            _worktree.remove(BARE, wtdir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== testgraph accuracy (recall-first) ===")
    for r in rows:
        if len(r) == 3:
            print(f"  {r[0]}  {r[1]}: {r[2]}")
            continue
        sha, desc, oracle, selected, mstr = r
        print(f"\n  {sha}  {desc}")
        print(f"    oracle   : {oracle}")
        print(f"    selected : {selected}")
        print(f"    {mstr}")
    mean_p = sum(precisions) / len(precisions) if precisions else float("nan")
    print("\n--- summary ---")
    print(f"  MIN RECALL : {min_recall:.2f}   (S1 requires 1.00)")
    print(f"  mean precision (non-empty oracles): {mean_p:.2f}")
    print(f"  S1 VERDICT : {'PASS' if min_recall >= 1.0 else 'FAIL'}")
    return 0 if min_recall >= 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
