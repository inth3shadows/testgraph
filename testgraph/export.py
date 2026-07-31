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
import subprocess
import sys

from . import db as dbmod
from . import integrity
from . import registry as reg
from . import select as sel


def _map_relevant(path):
    """Could an uncommitted change to `path` make the map describe uncommitted code?

    Only for a file that can actually produce a row. Marking the tree dirty over an
    untracked `NOTES.md`, testgraph's own `.testgraph/journey-map.md`, or
    codegraph's `.codegraph/` would mark nearly every real export dirty —
    honeyslate has two untracked docs right now — and a marker that is always on is
    one the reader learns to skip past. That defeats the purpose it was added for.

    `_is_product`, not a bare extension check: `impacted_closure` walks *callers*,
    so a test symbol's closure never contains a journey entry and no test file has
    ever appeared in the map (honeyslate's has 21 sections, none of them `tests/`).
    A `.d.ts` has no nodes at all. An uncommitted edit to either cannot change a
    single row, and the stamp is baked into a persisted artifact — one regeneration
    with a stray test edit would tell every later reader to distrust it.
    """
    return sel._is_product(path)


class StampError(Exception):
    """The target's provenance could not be established."""


def commit_stamp(repo):
    """`<short sha>`, or `<short sha>-dirty` when the target tree has changes.

    Issue #25: this used to be a `subprocess.run` with no `check`, whose stdout
    fell back to the string `"unknown"`. A non-git `--repo`, or any git failure,
    therefore produced a map that *looked* stamped while the skill's staleness
    escalation ("`generated from commit` is far behind HEAD") had nothing to
    compare and silently never fired. Failing open on provenance is the same class
    of defect as answering `NONE` on an unmappable diff.

    The dirty marker matters because the map is built from the CodeGraph index,
    which can be ahead of the last commit: without it, a map describing
    uncommitted code claims clean provenance.
    """
    def git(*args):
        try:
            return subprocess.run(
                ["git", "-C", repo, *args], capture_output=True, text=True
            )
        except OSError as exc:
            # git absent from PATH raises rather than returning non-zero, so
            # without this the export dies with a traceback and exit 1 instead of
            # the "map NOT written" / exit 2 contract this function exists to keep.
            raise StampError(f"cannot run git for {repo}: {exc}") from exc

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise StampError(
            f"{repo} is not a git working tree "
            f"({inside.stderr.strip() or 'git reported no working tree'}) — the "
            f"map's staleness check needs a real commit to compare against"
        )
    # `git -C` walks UP to an enclosing repository, so a plain directory nested
    # inside one answers `rev-parse HEAD` with THAT repo's commit. A map stamped
    # from a different project is worse than an unstamped one: the consumer's
    # "far behind HEAD" comparison runs against a history that keeps moving, so
    # the map reads fresh forever. A target with no tracked files under it is not
    # something the enclosing repo describes.
    tracked = git("ls-files")
    if tracked.returncode != 0 or not tracked.stdout.strip():
        raise StampError(
            f"{repo} has no git-tracked files, so the enclosing repository's HEAD "
            f"is not this target's provenance — stamping it would claim a commit "
            f"from another project"
        )
    head = git("rev-parse", "--short", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        if git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
            # Unborn/orphan HEAD. Deliberately blocking rather than stamping
            # something like `unborn-dirty`: every row would describe code in no
            # commit, and the stamp's whole job is to be comparable to a history.
            raise StampError(
                f"the target ({repo}) has no commits yet — commit first; a map of "
                f"an unborn branch has no provenance to record"
            )
        raise StampError(
            f"cannot read the target's HEAD ({repo}): "
            f"{head.stderr.strip() or 'git produced no output'}"
        )
    sha = head.stdout.strip()
    # -uall: without it git collapses a wholly-untracked directory to `?? web/`,
    # which hides the extension — a brand-new `web/App.svelte` then read as
    # irrelevant and the tree looked clean. Caught by test, not by inspection.
    # `-- .`: scope to the target. For a subdirectory target the enclosing repo's
    # changes elsewhere are not this map's business.
    # `git status` prints paths relative to the REPO ROOT while `ls-files` above
    # prints them relative to the cwd — two guards on different bases. For a target
    # like `<repo>/e2e/app` every status path gained a leading `e2e/`, `_is_test`
    # matched that segment, and the map was stamped clean no matter what was
    # uncommitted: the exact "clean provenance for uncommitted code" bug this
    # function exists to prevent. `--show-prefix` gives the offset to strip.
    prefix = git("rev-parse", "--show-prefix").stdout.strip()
    status = git("status", "--porcelain", "-uall", "--", ".")
    if status.returncode != 0:
        raise StampError(
            f"cannot read the target's working-tree state ({repo}): "
            f"{status.stderr.strip() or 'git failed'}"
        )
    for line in status.stdout.splitlines():
        paths = line[3:].strip().strip('"').split(" -> ")  # renames carry both
        rel = [
            p[len(prefix):] if prefix and p.startswith(prefix) else p
            for p in (q.strip().strip('"') for q in paths) if p
        ]
        if any(_map_relevant(p) for p in rel):
            return f"{sha}-dirty"
    return sha


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
        for enid, jids in entry_map.items():
            if enid in impacted:
                for jid in jids:
                    # best (highest) confidence across this journey's entries
                    hits[jid] = max(hits.get(jid, 0.0), impacted[enid])
        if not hits:
            continue
        rows_by_file.setdefault(path, []).append(
            {
                "symbol": name,
                "kind": kind,
                "lines": [lo, hi],
                "journeys": sorted(hits, key=reg.journey_sort_key),
                "confidence": {
                    j: round(hits[j], 3)
                    for j in sorted(hits, key=reg.journey_sort_key)
                },
                "verify_manually": sorted(
                    (j for j, c in hits.items() if c <= dbmod.LOW_CONFIDENCE),
                    key=reg.journey_sort_key,
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
    ]
    # Integrity warnings belong IN the artifact, not only on the stderr of the run
    # that produced it (issue #23). Only *blocking* problems stopped the write;
    # warnings — "index unpinned", "N source files newer than the index", i.e. the
    # exact "this map under-reports" signal — went to a terminal nobody will ever
    # see again. That contradicted this module's own rationale for blocking on a
    # corrupt index: the file outlives the run and carries no warning of its own.
    if meta.get("warnings"):
        out.append(
            "> **The index was not fully trustworthy when this map was "
            "generated.** These warnings were raised at generation time and are "
            "reproduced here because this file outlives the run that made it. A "
            "stale or unverified index makes the map *under-report* — a symbol "
            "missing from it may still reach journeys."
        )
        out.append(">")
        for w in meta["warnings"]:
            out.append(f"> - {w}")
        out.append("")
    # Its own banner, deliberately not merged into the one above. The index can be
    # perfect and this map still be incomplete, because nobody checked that the
    # registry lists every journey the app has. Different cause, different remedy,
    # so a reader is never sent to re-index over a review that was never done.
    if meta.get("registry_approval"):
        out += [
            "> **The journey registry behind this map was not human-approved.** "
            f"{meta['registry_approval']}. The index may be sound and the map "
            "still incomplete: a journey nobody registered has no rows here, so "
            "a symbol's *absence* from every journey means **unknown**, not "
            "*unaffected*. Re-indexing does not change this — reviewing and "
            "approving the registry does.",
            "",
        ]
    if str(meta["commit"]).endswith("-dirty"):
        # The map is built from the CodeGraph index, which can be ahead of the
        # last commit. Without saying so, a map describing uncommitted code claims
        # clean provenance and the reader cannot tell (issue #25).
        out += [
            "> **`-dirty`: the target had uncommitted changes when this was "
            "generated.** Rows may describe code that is not in any commit, and "
            "the line hints are shifted by whatever is still unstaged. Treat a "
            "missing symbol as *unknown* and regenerate after committing.",
            "",
        ]
    out += [
        "Look up the symbols you changed **by name**. Every journey listed for "
        "them may have changed behavior and is worth verifying. This is "
        "**recall-first**: a shared symbol legitimately fans out to many "
        "journeys.",
        "",
        "**Line numbers are frozen at the commit above and are a hint only** — "
        "your own edit has already shifted them, so an insertion higher up the "
        "file makes the ranges point at the wrong symbol (issue #24). Match the "
        "symbol name first, and fall back to the range when you cannot: import "
        "nodes and module-level bindings get rows too, and an edit to one of "
        "those looks like no symbol you touched. An edit you cannot attribute to "
        "any row is *unknown*, never *no journeys*.",
        "",
        "`!` marks a journey reached only through weak or synthesized graph edges "
        "— treat it as *verify manually*, not as *probably fine*.",
        "",
        "## Journeys",
        "",
    ]
    for jid, spec in sorted(
        registry["journeys"].items(), key=lambda kv: reg.journey_sort_key(kv[0])
    ):
        entries = ", ".join(f"`{e['name']}`" for e in spec["entries"])
        out.append(f"- **{jid}** {spec['name']} — entry: {entries}")
    out.append("")
    out.append("## Symbols by file")
    for path in sorted(rows_by_file):
        out.append("")
        out.append(f"### `{path}`")
        out.append("")
        # Symbol first, deliberately. The lookup key is the symbol name (issue
        # #24: line numbers are stale the moment the agent edits the file), and a
        # table that leads with `lines` invites exactly the lookup the skill's
        # rules forbid. The column header carries the caveat too, since an agent
        # may land on one `###` section without reading the preamble.
        out.append("| symbol | journeys | lines (at generation — stale hint) |")
        out.append("|---|---|---|")
        for r in rows_by_file[path]:
            js = " ".join(
                f"{j}!" if j in r["verify_manually"] else j for j in r["journeys"]
            )
            out.append(f"| `{r['symbol']}` | {js} | {r['lines'][0]}–{r['lines'][1]} |")
    out.append("")
    # Footnote, deliberately not the "not fully trustworthy" banner: an entry no
    # parser covers is a permanent, unfixable-by-re-indexing gap, so it is stated
    # once as a limitation rather than as an integrity alarm. Previously this rode
    # in `meta` and was never rendered at all — dead data behind a claim.
    if meta.get("unchecked_entries"):
        out.append(
            "_Entry symbols not verified against source (no parser for the file "
            "type), so drift in them cannot be detected: "
            + ", ".join(f"{jid} `{name}` ({rel})"
                        for jid, name, rel in meta["unchecked_entries"])
            + "._"
        )
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
        # Directory creation is deferred until just before the write: creating it
        # here would leave an empty `.testgraph/` in the target repo after a run
        # that printed "map NOT written", and it is deliberately not gitignored.
        args.out = os.path.join(args.repo, ".testgraph", "journey-map.md")

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
    # Live-drift BEFORE the print loop: appended after it, drift warnings reached
    # neither the terminal nor — on a run that then blocked — the file, discarding
    # the one signal that the registry is stale on exactly the runs most likely to
    # carry it. Same live parse the selector runs (issue #7).
    for jid, name, rel, why in reg.live_drift(args.repo, registry):
        warnings.append(
            f"journey {jid} entry `{name}` ({rel}): {why} — {reg.remedy_for(why)}"
        )
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    # Registry provenance is NOT an integrity warning and must not ride that
    # channel: `render_markdown` prints every `warnings` entry under "The index
    # was not fully trustworthy", and the consumer skill reads that banner as
    # "regenerate after `codegraph index`". An unapproved registry has no index
    # component, so it would assert a permanent index problem with a remedy that
    # can never clear it — the failure `unchecked_entries` was split out to
    # avoid. Own field, own blockquote, own remedy.
    approval = reg.approval_warning(registry)
    if approval:
        print(f"WARN: {approval}", file=sys.stderr)
    # Provenance is checked alongside index integrity, and blocks for the same
    # reason: a map whose stamp cannot be trusted disables the consumer's only
    # staleness escalation, and the file outlives the run (issue #25).
    stamp, provenance = None, []
    try:
        stamp = commit_stamp(args.repo)
    except StampError as exc:
        # Kept out of `blocking`: that list prints under "index not trustworthy",
        # whose remedy is a multi-minute `codegraph index` rebuild. A --repo that
        # simply is not a git repo has nothing to do with the index, and sending
        # the reader to rebuild it wastes their time on the wrong fix.
        provenance.append(str(exc))
    # A journey whose entries no longer resolve vanishes from every row while the
    # legend keeps advertising it. A persisted map that lies is worse than none.
    for jid, names in reg.unresolved(conn, registry):
        blocking.append(
            f"journey {jid} ({reg.journey_name(registry, jid)}) has no resolvable "
            f"entry symbol ({', '.join(names)}) — registry is stale against the "
            f"index; it would silently vanish from every row"
        )

    if provenance:
        print("BLOCKED — provenance unverifiable; map NOT written", file=sys.stderr)
        for p in provenance:
            print(f"  x {p}", file=sys.stderr)
    if blocking:
        print("BLOCKED — index not trustworthy; map NOT written", file=sys.stderr)
        for b in blocking:
            print(f"  x {b}", file=sys.stderr)
    if provenance or blocking:
        return 2

    rows_by_file = build_map(conn, registry)
    meta = {
        "repo": args.repo,
        "schema": dbmod.schema_version(conn),
        "commit": stamp,
        "symbols": sum(len(v) for v in rows_by_file.values()),
        # carried into the artifact AND the --json sidecar, so a consumer of
        # either can tell the map was generated off a not-fully-trusted index
        "warnings": warnings,
        # a footnote in the map, never the "not fully trustworthy" banner: no
        # amount of re-indexing clears an unverifiable entry
        "unchecked_entries": reg.unchecked_entries(registry),
        # same rule, different cause: whether a human ever reviewed the registry
        # is a provenance fact about the map's INPUT, not about the index
        "registry_approval": approval,
    }

    md = render_markdown(rows_by_file, registry, meta)
    if args.out:
        parent = os.path.dirname(args.out)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
