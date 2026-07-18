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
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from testgraph import select as sel  # noqa: E402

BARE = "/home/ericm/personal_projects/honeyslate/.bare"
REGISTRY = os.path.join(ROOT, "journeys", "honeyslate.json")
CODEGRAPH = os.path.expanduser("~/.local/bin/codegraph")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


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
            wt = os.path.join(tmp, sha)
            add = run(["git", "-C", BARE, "worktree", "add",
                       "--detach", wt, sha])
            if add.returncode:
                rows.append((sha, "WORKTREE-FAIL", add.stderr.strip()[:60]))
                continue
            idx = run([CODEGRAPH, "init", wt])
            db = os.path.join(wt, ".codegraph", "codegraph.db")
            if not os.path.exists(db):
                rows.append((sha, "INDEX-FAIL", idx.stderr.strip()[:60]))
                run(["git", "-C", BARE, "worktree", "remove", "--force", wt])
                continue
            result = sel.select(wt, f"{sha}~1", sha, db, REGISTRY)
            if result["status"] != "OK":
                rows.append((sha, "BLOCKED", "; ".join(result.get("blocking", []))[:60]))
                run(["git", "-C", BARE, "worktree", "remove", "--force", wt])
                continue
            selected = [j["id"] for j in result["journeys"]]
            recall, precision, fp = score(selected, oracle)
            min_recall = min(min_recall, recall)
            if oracle:
                precisions.append(precision)
            rows.append((sha, c["desc"][:34], oracle, sorted(selected),
                         f"R={recall:.2f} P={precision:.2f} FP={fp}"))
            run(["git", "-C", BARE, "worktree", "remove", "--force", wt])
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
