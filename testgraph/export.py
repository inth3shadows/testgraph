"""testgraph export — a static journey map an agent reads with no graph at hand.

Why static. TDAD (arXiv:2603.17973v2) ships its impact analysis to coding agents
as a lightweight skill reading a pre-computed file, deliberately avoiding a graph
database or API call at agent runtime. The same reasoning applies here, harder:
an agent mid-edit should not need a live CodeGraph index, a Python environment,
or a working `.codegraph/` to know which journeys its change endangers.

Why it is worth doing at all: TDAD's *TDD Prompting Paradox* — telling an agent
to verify WITHOUT telling it what to verify made regressions worse than baseline
(9.94% vs 6.08%). The map is the missing target.

The map is the reverse index of the selector: symbol -> journeys that depend on
it, grouped by file so an agent can scan the files it just touched.

Consistency by construction. Each row is computed by running the SAME
`impacted_closure` the selector runs, seeded with that one symbol, and asking
which journey entries land in it. A cleverer inverse traversal would be faster
and would risk disagreeing with `select` — and a map that disagrees with the
selector is worse than no map, because an agent would trust it.

Usage:
    python -m testgraph.export --repo <path> [--out PATH] [--json PATH]
"""
import argparse
import json
import os
import sys

from . import db as dbmod
from . import integrity
from . import registry as reg


def build_map(conn, registry):
    """{file_path: [row, ...]} where each row is a symbol and the journeys that
    depend on it. Rows carry the weakest-link confidence of the best path, so an
    agent sees the same `verify manually` signal the CLI prints."""
    entry_map = reg.resolve_entries(conn, registry)
    rows_by_file = {}
    for nid, kind, name, path, lo, hi in conn.execute(
        "SELECT id, kind, name, file_path, start_line, end_line FROM nodes "
        "WHERE kind != 'file' AND file_path IS NOT NULL"
    ):
        impacted = dbmod.impacted_closure(conn, {nid})
        hits = {}
        for enid, jid in entry_map.items():
            if enid in impacted:
                # best (highest) confidence across this journey's entries
                hits[jid] = max(hits.get(jid, 0.0), impacted[enid])
        if not hits:
            continue
        rows_by_file.setdefault(path, []).append(
            {
                "symbol": name,
                "kind": kind,
                "lines": [lo, hi],
                "journeys": sorted(hits),
                "confidence": {j: round(c, 3) for j, c in sorted(hits.items())},
                "verify_manually": sorted(
                    j for j, c in hits.items() if c <= dbmod.LOW_CONFIDENCE
                ),
            }
        )
    for rows in rows_by_file.values():
        rows.sort(key=lambda r: (r["lines"][0], r["symbol"]))
    return rows_by_file


def render_markdown(rows_by_file, registry, meta):
    out = [
        "# Journey map — which user journeys depend on which symbols",
        "",
        f"Target: `{meta['repo']}` · index schema {meta['schema']} · "
        f"generated from commit `{meta['commit']}`",
        "",
        "Look up the symbols you changed. Every journey listed for them may have "
        "changed behavior and is worth verifying. This is **recall-first**: a "
        "shared symbol legitimately fans out to many journeys.",
        "",
        "`!` marks a journey reached only through weak or synthesized graph edges "
        "— treat it as *verify manually*, not as *probably fine*.",
        "",
        "## Journeys",
        "",
    ]
    for jid, spec in sorted(registry["journeys"].items()):
        entries = ", ".join(f"`{e['name']}`" for e in spec["entries"])
        out.append(f"- **{jid}** {spec['name']} — entry: {entries}")
    out.append("")
    out.append("## Symbols by file")
    for path in sorted(rows_by_file):
        out.append("")
        out.append(f"### `{path}`")
        out.append("")
        out.append("| lines | symbol | journeys |")
        out.append("|---|---|---|")
        for r in rows_by_file[path]:
            js = " ".join(
                f"{j}!" if j in r["verify_manually"] else j for j in r["journeys"]
            )
            out.append(f"| {r['lines'][0]}–{r['lines'][1]} | `{r['symbol']}` | {js} |")
    out.append("")
    out.append(
        f"_{meta['symbols']} symbols across {len(rows_by_file)} files reach at "
        f"least one journey. Symbols reaching none are omitted._"
    )
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testgraph.export")
    ap.add_argument("--repo", default="/home/ericm/personal_projects/honeyslate/main")
    ap.add_argument("--db", default=None)
    ap.add_argument(
        "--registry",
        default=os.path.join(os.path.dirname(__file__), "..", "journeys",
                             "honeyslate.json"),
    )
    ap.add_argument("--out", default=None, help="markdown output path (default stdout)")
    ap.add_argument(
        "--into-target", action="store_true",
        help="write to <repo>/.testgraph/journey-map.md so the map lives in the "
             "repo it describes and the agent skill finds it without a "
             "central-store lookup (issue #20)",
    )
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    if args.into_target:
        if args.out:
            print("--out and --into-target are mutually exclusive", file=sys.stderr)
            return 2
        args.out = os.path.join(args.repo, ".testgraph", "journey-map.md")
        os.makedirs(os.path.dirname(args.out), exist_ok=True)

    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    conn = dbmod.connect(db_path)
    registry = reg.load(args.registry)

    # An export off a corrupt index is exactly as dangerous as a selection off
    # one — more so, because the file outlives the run and carries no warning.
    blocking, warnings = integrity.check(
        conn,
        args.repo,
        registry.get("spot_checks", {}),
        schema_pin=registry.get("codegraph_schema_version"),
    )
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if blocking:
        print("BLOCKED — index not trustworthy; map NOT written", file=sys.stderr)
        for b in blocking:
            print(f"  x {b}", file=sys.stderr)
        return 2

    rows_by_file = build_map(conn, registry)
    import subprocess

    commit = subprocess.run(
        ["git", "-C", args.repo, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() or "unknown"
    meta = {
        "repo": args.repo,
        "schema": dbmod.schema_version(conn),
        "commit": commit,
        "symbols": sum(len(v) for v in rows_by_file.values()),
    }

    md = render_markdown(rows_by_file, registry, meta)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"wrote {args.out} ({meta['symbols']} symbols, "
              f"{len(rows_by_file)} files)")
    else:
        sys.stdout.write(md)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta, "files": rows_by_file}, fh, indent=2)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
