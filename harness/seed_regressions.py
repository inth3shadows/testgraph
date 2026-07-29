"""Seeded-regression eval (issue #5) — the experiment that can falsify recall.

The S1 harness scores 5 hand-labeled real commits. Most real commits break
nothing, so that set is small AND easy: the parent plan itself calls it a weak
experiment. This one manufactures the hard cases.

Method. Check honeyslate out once at a fixed base and index it once. Then, for
each of ~N sampled functions across the product source, edit one line INSIDE
that function, commit, and ask the selector which journeys the change endangers.
Ground truth comes from `ast_oracle` — a call graph built from source text by
Python's own parser, walked forward from each journey entry, i.e. independently
of the CodeGraph edges the selector walks backward. Then reset and repeat.

The line edit preserves the function's line count so the base index stays
aligned and no re-index is needed per site (the S1 harness re-indexes per
commit; here the structure never changes, only one line's text).

WHAT THIS MEASURES: selection. Given a change at a known location, does the
selector name the journeys that can reach it, and at what rank?

WHAT IT DOES NOT MEASURE: detection. Whether a *behavioral* mutation would
actually fail the journey requires running journeys, which needs the seeded,
resettable environment decided in issue #8 and not yet built. Until then a
seeded diff and a seeded bug are indistinguishable to the selector, because the
selector only ever reads the diff.

Disagreements are reported, not reconciled. Tuning the oracle until it agrees
with the selector would destroy the only reason the oracle is independent.

Usage: python3 harness/seed_regressions.py [--sites N] [--base SHA]
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

from harness import ast_oracle  # noqa: E402
from testgraph import select as sel  # noqa: E402

BARE = "/home/ericm/personal_projects/honeyslate/.bare"
REGISTRY = os.path.join(ROOT, "journeys", "honeyslate.json")
CODEGRAPH = os.path.expanduser("~/.local/bin/codegraph")
MARK = "  # SEEDED-MUTANT"


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def pick_sites(sites, want):
    """Deterministic spread across files — no RNG, so runs are reproducible."""
    ordered = sorted(sites.items())
    if len(ordered) <= want:
        return ordered
    stride = len(ordered) / float(want)
    return [ordered[int(i * stride)] for i in range(want)]


def mutate(path, start, end):
    """Edit one line inside [start, end] (1-indexed, inclusive), preserving the
    line count. Returns the 1-indexed line touched, or None if no body line is
    suitable."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    # walk backward from the end of the body for a substantive line: skip
    # blanks, comments, and the `def` line itself.
    for idx in range(min(end, len(lines)) - 1, start - 1, -1):
        raw = lines[idx].rstrip("\n")
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("def ") or s.startswith(
            "async def "
        ) or s.startswith("@") or MARK in raw:
            continue
        lines[idx] = raw + MARK + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        return idx + 1
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", type=int, default=20)
    ap.add_argument("--base", default="main")
    ap.add_argument("--strict-adjudications", action="store_true",
                    help="fail if any excuse was adjudicated against a "
                         "different base commit")
    args = ap.parse_args(argv)

    registry = json.load(open(REGISTRY))
    adj = json.load(open(os.path.join(HERE, "adjudications.json")))["verdicts"]
    tmp = tempfile.mkdtemp(prefix="tg-seed-")
    wt = os.path.join(tmp, "wt")
    rows, misses, blocked, stale = [], [], [], []
    try:
        add = run(["git", "-C", BARE, "worktree", "add", "--detach", wt, args.base])
        if add.returncode:
            print("worktree add failed:", add.stderr.strip())
            return 2
        run(["git", "-C", wt, "config", "user.email", "seed@harness"])
        run(["git", "-C", wt, "config", "user.name", "seed harness"])
        base_sha = run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()

        idx = run([CODEGRAPH, "init", wt])
        db = os.path.join(wt, ".codegraph", "codegraph.db")
        if not os.path.exists(db):
            print("index failed:", idx.stderr.strip()[:200])
            return 2

        oracle, sites = ast_oracle.journey_oracle(wt, registry)
        chosen = pick_sites(sites, args.sites)
        print(f"=== seeded-regression eval: {len(chosen)} sites @ {base_sha[:7]} ===")

        for (rel, fname), (start, end) in chosen:
            full = os.path.join(wt, rel)
            touched = mutate(full, start, end)
            if touched is None:
                rows.append((rel, fname, "SKIP", "no mutable body line", None, None))
                run(["git", "-C", wt, "checkout", "--", "."])
                continue
            run(["git", "-C", wt, "commit", "-qam", f"seed {rel}:{fname}"])

            expected = sorted(j for j, names in oracle.items() if fname in names)
            result = sel.select(wt, base_sha, "HEAD", db, REGISTRY)
            if result["status"] != "OK":
                blocked.append((rel, fname, "; ".join(result.get("blocking", []))))
                run(["git", "-C", wt, "reset", "-q", "--hard", base_sha])
                continue

            ranked = [j["id"] for j in result["journeys"]]
            got = set(ranked)
            missing = sorted(set(expected) - got)

            # Adjudicate before scoring. A raw disagreement is a QUESTION, not a
            # verdict: the name-keyed oracle over-approximates by construction,
            # so "oracle reachable, selector silent" may mean the oracle invented
            # a path. Only a recorded 'selector-miss' counts against recall.
            verdict = adj.get(f"{rel}:{fname}")
            excused = set()
            if verdict and verdict.get("verdict") == "oracle-false-positive":
                excused = set(verdict.get("journeys", []))
                # An excuse is a claim about a call path at a point in time. If
                # the code has moved, the path may now exist and the excuse would
                # suppress a REAL miss -- the failure mode this project exists to
                # prevent, reintroduced inside its own harness. Report loudly.
                if not base_sha.startswith(verdict.get("base_sha", "")):
                    stale.append((rel, fname, verdict.get("base_sha", "?")))
            unexcused = [j for j in missing if j not in excused]
            if missing:
                misses.append((rel, fname, missing, ranked, sorted(excused),
                               unexcused, bool(verdict)))

            adjudged_expected = [j for j in expected if j not in excused]
            hit = set(adjudged_expected) & got
            recall = (
                len(hit) / len(adjudged_expected) if adjudged_expected else 1.0
            )
            # worst rank among expected journeys: how deep must you read?
            worst = max((ranked.index(j) + 1 for j in hit), default=0)
            rows.append((rel, fname, f"{recall:.2f}", expected, ranked, worst))
            run(["git", "-C", wt, "reset", "-q", "--hard", base_sha])
    finally:
        run(["git", "-C", BARE, "worktree", "remove", "--force", wt])
        shutil.rmtree(tmp, ignore_errors=True)

    scored = [r for r in rows if r[2] not in ("SKIP",)]
    print(f"\n{'file:function':<46} {'R':>5}  expected -> selected (worst rank)")
    for rel, fname, rec, exp, ranked, worst in rows:
        if rec == "SKIP":
            print(f"  {rel}:{fname:<40} SKIP  {exp}")
            continue
        print(f"  {rel}:{fname:<40} {rec:>5}  {exp} -> {len(ranked)} sel, rank<={worst}")

    recalls = [float(r[2]) for r in scored]
    with_exp = [r for r in scored if r[3]]
    print("\n--- summary ---")
    print(f"  sites scored        : {len(scored)}  (skipped {len(rows) - len(scored)})")
    print(f"  sites with a nonempty oracle: {len(with_exp)}")
    print(f"  MIN RECALL          : {min(recalls, default=float('nan')):.2f}")
    print(f"  mean recall         : "
          f"{(sum(recalls) / len(recalls) if recalls else float('nan')):.2f}")
    if with_exp:
        worsts = [r[5] for r in with_exp if r[5]]
        print(f"  mean worst rank     : "
              f"{(sum(worsts) / len(worsts) if worsts else float('nan')):.2f}")
        print(f"  mean journeys named : "
              f"{sum(len(r[4]) for r in with_exp) / len(with_exp):.2f} of "
              f"{len(registry['journeys'])}")
    if blocked:
        print(f"  BLOCKED sites       : {len(blocked)}")
        for rel, fname, why in blocked[:5]:
            print(f"    {rel}:{fname} — {why[:70]}")
    open_misses = [m for m in misses if m[5]]
    print(f"\n  DISAGREEMENTS (oracle reachable, selector silent): {len(misses)}")
    for rel, fname, missing, ranked, excused, unexcused, known in misses:
        tag = "ADJUDICATED oracle-FP" if not unexcused else (
            "UNADJUDICATED" if not known else "CONFIRMED SELECTOR MISS")
        print(f"    [{tag}] {rel}:{fname}")
        print(f"        oracle says {missing}, selector said {ranked}")
        if excused:
            print(f"        excused by harness/adjudications.json: {excused}")
    print(f"  adjudicated as oracle false positives: "
          f"{len(misses) - len(open_misses)} / {len(misses)}")
    if stale:
        print(f"\n  STALE EXCUSES ({len(stale)}) — adjudicated against a different"
              f" base; re-read the call path:")
        for rel, fname, was in stale:
            print(f"    {rel}:{fname} (adjudicated @ {was}, run @ {base_sha[:7]})")
    if open_misses:
        print("\n  OPEN: read each call path. If a real path exists it is a recall")
        print("  bug — fix the closure. If not, record the evidence in")
        print("  harness/adjudications.json. Do NOT tune the oracle to agree.")

    ok = bool(recalls) and min(recalls) >= 1.0 and not open_misses
    if stale and args.strict_adjudications:
        ok = False
    print(f"\n  VERDICT : {'PASS' if ok else 'FAIL'}  "
          f"(adjudicated recall must be 1.00; no open disagreements)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
