"""Split a `propose` draft into two registries of equal size that differ ONLY in
how much code their journeys share.

Why this exists
---------------
TECHNICAL.md records that signedintake's selectivity IMPROVED when its registry
grew 8 -> 14 journeys (72.3% -> 81.1% of journey-runs avoided) and names the
mechanism: the journeys added sit OFF the shared-symbol paths, so they dilute the
fan-out. It then predicts, in writing, that "the effect reverses for a registry
grown ALONG those paths instead."

Testing that by hand-picking journeys would prove nothing — the prediction is
already published, so the author knows which answer to produce. This module is
the mechanical alternative: one stated rule, run over every drafted handler,
producing a HIGH-coupling arm and a LOW-coupling arm of the SAME size from the
SAME repo. Registry size is held constant so coupling is the only variable.

The scoring below does not have to be a perfect model of selection. It only
decides which handler lands in which arm; the measurement itself runs the real,
unmodified `testgraph.select` through `selectivity.py`.

Dependency footprint
--------------------
`db.impacted_closure` answers "who is affected if this changes" by walking edges
BACKWARD (target -> source) plus a file-contains expansion. This module needs the
inverse question — "what could change and thereby select this journey" — so it
inverts that derivation exactly:

    impacted:  t in I, edge(s -> t, REACH_KIND)     => s in I
               f in I, f is a file, contains(f -> x) => x in I

    inverted:  from n, add every t with edge(n -> t, REACH_KIND)
               from n, add the file f with contains(f -> n)

Both rules are the contrapositive of the two `impacted_closure` propagation
rules, so `Dep(E)` is the set whose members would put `E` in the impacted
closure. Walked in memory rather than as 207 recursive CTEs: the graph is ~25k
edges, which is nothing, and a plain BFS is far easier to check by eye than SQL
that has to be read in reverse.

Usage: python3 harness/couple.py --draft PATH --db PATH --out-dir DIR [--size 22]
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from testgraph import db as dbmod  # noqa: E402

# Share of handlers a node must appear in before it counts as "shared core".
# 0.5 is a deliberate midpoint: at 0.9 the core collapses to the handful of
# framework symbols every route touches (which cannot discriminate between
# handlers), and at 0.1 it swells to include most of the app (same problem, other
# end). Reported alongside the result so a reader can see what it selected.
CORE_SHARE = 0.5


def load_graph(conn):
    """(reach, contained_by) adjacency for the two inverted rules.

    reach[n]        -> nodes n depends on directly (edge n -> t over REACH_KINDS)
    contained_by[n] -> the file node that `contains` n
    """
    reach = collections.defaultdict(set)
    kinds = ",".join("'%s'" % k for k in dbmod.REACH_KINDS)
    for source, target in conn.execute(
        f"SELECT source, target FROM edges WHERE kind IN ({kinds})"
    ):
        reach[source].add(target)

    contained_by = {}
    for source, target in conn.execute(
        "SELECT source, target FROM edges WHERE kind = 'contains'"
    ):
        # Only file containment participates in `impacted_closure`'s rule 2, so
        # only file containment inverts. A class containing a method is a
        # `contains` edge too and must not be walked here, or every method would
        # drag its whole class into the footprint by structure alone.
        if source.startswith("file:"):
            contained_by[target] = source
    return reach, contained_by


def footprint(start_ids, reach, contained_by):
    """`Dep(E)` — everything whose change would put one of `start_ids` in the
    impacted closure. Seeds included; BFS, cycle-safe by the seen set."""
    seen = set(start_ids)
    queue = collections.deque(start_ids)
    while queue:
        n = queue.popleft()
        nxt = set(reach.get(n, ()))
        f = contained_by.get(n)
        if f:
            nxt.add(f)
        for t in nxt:
            if t not in seen:
                seen.add(t)
                queue.append(t)
    return seen


def score_journeys(conn, draft):
    """[(jid, overlap, footprint_size)] plus the shared core, for every journey
    whose entries resolve. Unresolvable journeys are dropped and counted —
    including one in an arm would hand `selectivity.py` a journey that can never
    be selected, which reads as narrow selection rather than a broken row."""
    reach, contained_by = load_graph(conn)

    footprints, dropped = {}, []
    for jid, journey in draft["journeys"].items():
        ids = []
        for entry in journey["entries"]:
            ids.extend(dbmod.resolve_symbol(conn, entry["name"], entry.get("file")))
        if not ids:
            dropped.append(jid)
            continue
        footprints[jid] = footprint(ids, reach, contained_by)

    counts = collections.Counter()
    for dep in footprints.values():
        counts.update(dep)
    threshold = CORE_SHARE * len(footprints)
    core = {n for n, c in counts.items() if c >= threshold}

    scored = []
    for jid, dep in footprints.items():
        scored.append((jid, len(dep & core), len(dep)))
    # Ranked on the COUNT of shared-core nodes a journey depends on, not on the
    # fraction of its footprint that is core.
    #
    # The fraction was the first rule written, and measuring it is what showed it
    # to be wrong — before any sweep ran, so no outcome informed this change. The
    # core is only 77 nodes and 174 of 207 handlers reach >= 90% of it, so the
    # fraction is dominated by its denominator: it ranks a handler with a large
    # PRIVATE footprint as "low coupling" even though that handler depends on
    # nearly the whole core too. That produced a LOW arm averaging 777-node
    # footprints against a HIGH arm averaging 88 — a 9x size difference standing
    # in for a coupling difference that was not really there (pearson(size,
    # fraction) = -0.83).
    #
    # The count asks the question the prediction is actually about: how much of
    # the shared spine does this journey sit on. Footprint size is the tiebreak,
    # smaller first, so a large private footprint can never buy a place in the
    # HIGH arm.
    scored.sort(key=lambda r: (-r[1], r[2], r[0]))
    return scored, core, dropped


def build_registry(draft, jids, target, note):
    """A registry carrying `jids` only, renumbered J1..Jn.

    Renumbered because `journey_sort_key` and every report downstream read the
    trailing number, and carrying `propose`'s route-derived ids (which are long
    and non-numeric) through would make the two arms unreadable side by side.
    `spot_checks` and `codegraph_schema_version` come straight from the draft so
    `integrity.check` has the same pins it drafted for itself.
    """
    journeys = {}
    for i, jid in enumerate(jids, start=1):
        src = draft["journeys"][jid]
        journeys[f"J{i}"] = {
            "name": src["name"],
            "entries": src["entries"],
            **({"route": src["route"]} if "route" in src else {}),
            "drafted_as": jid,
        }
    return {
        "target": target,
        "note": note,
        # Machine-derived and unreviewed by construction. `approval_warning`
        # fires, `select` warns and never blocks, and `selectivity.py` reports
        # sizes rather than recall — so false is the honest value, and setting it
        # true to quiet the warning would be the exact silent-confidence failure
        # this repo keeps designing against.
        "approved": False,
        "proposed_by": "harness/couple.py",
        "codegraph_schema_version": draft.get("codegraph_schema_version"),
        "journeys": journeys,
        "spot_checks": draft.get("spot_checks", {}),
        "spot_check_basis": draft.get("spot_check_basis"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness/couple.py")
    ap.add_argument("--draft", required=True, help="a testgraph.propose draft")
    ap.add_argument("--db", required=True, help="codegraph index for the same repo")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--target", default=None,
                    help="registry target (defaults to the draft's)")
    ap.add_argument("--size", type=int, default=22,
                    help="journeys per arm; 22 so the result still reads as "
                         "'20+' if one or two fail to resolve at older commits")
    args = ap.parse_args(argv)

    draft = json.load(open(args.draft))
    target = args.target or draft.get("target")
    conn = dbmod.connect(args.db)
    scored, core, dropped = score_journeys(conn, draft)

    if len(scored) < 2 * args.size:
        print(f"only {len(scored)} resolvable journeys — need {2 * args.size} "
              f"for two disjoint arms of {args.size}", file=sys.stderr)
        return 1

    # Each arm is sorted so its own tiebreak prefers the SMALLEST footprint.
    # Slicing one sorted list from both ends looks equivalent and is not: it
    # hands the LOW arm the largest footprint in each core tier, which is how a
    # 1292-node outlier landed in it on the first run. Footprint size drives
    # selection probability on its own, so it has to be pushed away from both
    # arms, not just the one that happens to be at the head of the list.
    high = sorted(scored, key=lambda r: (-r[1], r[2], r[0]))[:args.size]
    low = sorted(scored, key=lambda r: (r[1], r[2], r[0]))[:args.size]
    overlap_ids = {r[0] for r in high} & {r[0] for r in low}
    if overlap_ids:
        print(f"arms are not disjoint ({len(overlap_ids)} shared) — the repo "
              f"cannot supply two distinct arms at size {args.size}",
              file=sys.stderr)
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    written = []
    for arm, rows, blurb in (
        ("high", high, "the %d handlers depending on the MOST shared-core nodes "
                       "— a registry grown ALONG the shared symbol paths"),
        ("low", low, "the %d handlers depending on the FEWEST shared-core nodes "
                     "— the signedintake condition (isolated journeys) at the "
                     "same registry size"),
    ):
        note = (
            (blurb % args.size)
            + f". Derived mechanically by harness/couple.py from a "
              f"testgraph.propose draft of {len(draft['journeys'])} handlers "
              f"({len(scored)} resolvable); shared core = nodes in >= "
              f"{CORE_SHARE:.0%} of footprints ({len(core)} nodes). Selection "
              f"rule pre-registered before the sweep ran — see "
              f"~/.claude/plans/testgraph-20plus-journey-falsification.md. "
              f"UNAPPROVED: a measurement artifact, not a product registry."
        )
        reg = build_registry(draft, [r[0] for r in rows], target, note)
        path = os.path.join(args.out_dir, f"{target}-{arm}.json")
        with open(path, "w") as fh:
            json.dump(reg, fh, indent=2)
            fh.write("\n")
        written.append((arm, path, rows))

    print(f"draft journeys      : {len(draft['journeys'])}")
    print(f"resolvable          : {len(scored)}  (dropped {len(dropped)})")
    print(f"shared core         : {len(core)} nodes "
          f"(in >= {CORE_SHARE:.0%} of footprints)")
    # How separable the two arms actually are. If the floor is close to the
    # ceiling, this repo cannot supply a low-coupling registry at all — which is
    # a result about the codebase, and has to be visible in the output rather
    # than inferred later from the two files.
    reach_all = sum(1 for r in scored if r[1] >= 0.9 * len(core))
    print(f"journeys reaching >= 90% of core: {reach_all} of {len(scored)}")
    for arm, path, rows in written:
        ov = [r[1] for r in rows]
        fp = [r[2] for r in rows]
        print(f"\n  {arm.upper()} -> {path}")
        print(f"    core nodes: {min(ov)} .. {max(ov)} of {len(core)}  "
              f"(mean {sum(ov) / len(ov):.1f})")
        print(f"    footprint : {min(fp)} .. {max(fp)} nodes  "
              f"(mean {sum(fp) / len(fp):.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
