"""Index-integrity guard — refuses to select off an untrustworthy graph.

Motivated by a real 2026-07-17 incident: an interrupted `codegraph` run left
blast radius 85% wrong (get_settings: 3 callers vs 20 real), and `codegraph
sync` did NOT repair it — only a full `codegraph index` did. The pending-ref
and freshness checks are necessary but NOT sufficient (sync cleared the
pending warning while edges stayed wrong); the caller-count spot-check is what
actually catches that failure mode.

Returns (blocking_problems, warnings). A non-empty blocking list means: do not
answer — the remedy is a full `codegraph index` rebuild, never `sync`.
"""
import hashlib
import os

from . import db as dbmod


def stale_by_mtime(conn, repo_root):
    """Repo-relative paths whose file on disk is newer than its index row.

    Cheap and WRONG OFTEN, which is why it can only ever carry a warning:
    `git checkout` rewrites mtimes on every file it touches, so a branch switch
    marks half the tree "stale" without changing a byte. Free to compute over
    the whole repo, so it is worth what it costs — as a hint. The 2s slop
    absorbs filesystem timestamp granularity."""
    stale = set()
    for row in conn.execute("SELECT path, indexed_at FROM files"):
        p = os.path.join(repo_root, row["path"])
        idx = row["indexed_at"]
        if not idx or not os.path.exists(p):
            continue
        idx_s = idx / 1000.0 if idx > 1e12 else idx  # normalize ms -> s
        if os.path.getmtime(p) > idx_s + 2:
            stale.add(row["path"])
    return stale


def content_drift(conn, repo_root, paths):
    """Of `paths`, those whose bytes no longer hash to what the index recorded.

    Authoritative where `stale_by_mtime` only guesses: `files.content_hash` is
    the sha256 codegraph itself compares when deciding whether `sync` has work
    to do, so agreeing with it means agreeing with the indexer. Verified against
    a real index — all 98 rows matched sha256 of the raw bytes, including files
    whose mtime said "stale".

    It costs a read and a hash per path, so it is spent only on the files in the
    diff, where a wrong answer is a wrong SELECTION rather than a wrong warning.
    A path with no index row is not drift — `select` already treats "no nodes"
    as its own kind of unmappable — and neither is one that is gone from the
    working tree, which is the whole-file deletion path."""
    drifted = set()
    wanted = set(paths)
    if not wanted:
        return drifted
    for row in conn.execute("SELECT path, content_hash FROM files"):
        if row["path"] not in wanted or not row["content_hash"]:
            continue
        p = os.path.join(repo_root, row["path"])
        try:
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
        if digest != row["content_hash"]:
            drifted.add(row["path"])
    return drifted


def check(conn, repo_root, spot_checks, pending_max=0, schema_pin=None):
    blocking, warnings = [], []

    # 0. schema pin (plan risk R1). codegraph's SQLite layout is an internal
    #    contract, not a public API: a renamed column in a codegraph upgrade
    #    would make the closure query return wrong rows rather than error, and a
    #    confidently-narrow answer is the one failure mode a selector must never
    #    have. Block on drift; the registry carries the known-good version.
    found = dbmod.schema_version(conn)
    if schema_pin is None:
        warnings.append(
            f"codegraph schema version unpinned (index reports {found}) — add "
            f'"codegraph_schema_version": {found} to the registry to detect drift'
        )
    elif found is None:
        blocking.append(
            f"registry pins codegraph schema {schema_pin} but the index reports "
            f"no schema_versions row — cannot verify column semantics"
        )
    elif found != schema_pin:
        blocking.append(
            f"codegraph schema {found} != pinned {schema_pin} — column semantics "
            f"may have changed; re-verify testgraph's queries against the new "
            f"schema, then update the registry pin"
        )

    # 1. pending unresolved refs (terminal 'failed' refs are external stdlib —
    #    ignored; only non-terminal 'pending' indicates an incomplete index).
    try:
        pending = conn.execute(
            "SELECT count(*) FROM unresolved_refs WHERE status = 'pending'"
        ).fetchone()[0]
        if pending > pending_max:
            blocking.append(
                f"{pending} unresolved refs still pending (> {pending_max}) — "
                f"index is mid-resolution"
            )
    except Exception:
        pass  # older schema without status column

    # 2. freshness: any tracked source newer than its index row. WARN not BLOCK
    #    — repo-wide staleness costs precision, and blocking on it would refuse
    #    to answer for files nobody touched.
    #
    #    This used to justify WARN with "a slightly stale index degrades
    #    precision, not recall, and codegraph git hooks normally keep it synced."
    #    Both halves were false once the pre-push hook started running this in
    #    anger. Nothing keeps these indexes synced: the autosync installer rewrote
    #    honeyslate's post-commit and dropped the codegraph block, and
    #    signedintake never had one. And staleness in a CHANGED file is a recall
    #    problem, not a precision one — seeds come from line ranges, so lines that
    #    moved since indexing resolve to a neighbouring symbol or to none.
    #
    #    `select` now handles that case with `content_drift`, which is exact.
    #    This warning stays mtime-based and stays a warning: it is the cheap
    #    repo-wide hint, and it is wrong often enough (any `git checkout`) that
    #    nothing may be decided on it.
    #
    #    Every indexed language, not just python (issue #31). The python-only
    #    filter was consistent while selection ignored non-Python paths; once
    #    `select` seeds them (#21), a frontend file edited after the last index
    #    would otherwise produce a confidently narrow answer with no staleness
    #    warning at all.
    stale = sorted(stale_by_mtime(conn, repo_root))
    if stale:
        warnings.append(
            f"{len(stale)} source file(s) newer than the index "
            f"(e.g. {stale[0]}) — consider `codegraph sync`"
        )

    # 3. caller-count spot-check — the corruption detector `sync` can't clear.
    for name, spec in spot_checks.items():
        # A check whose edges are known-fabricated cannot detect corruption: it
        # passes on the fabrication exactly as it would on real callers (#66,
        # `ledger.append`). Counting it returns a green backed by nothing, which
        # is strictly worse than not checking — so a suspended check skips the
        # floor and surfaces its reason as a warning rather than being deleted,
        # which would take the reason with it.
        suspended = (spec.get("suspended") or "").strip()
        if suspended:
            warnings.append(f"spot-check '{name}' suspended — {suspended}")
            continue
        min_callers = spec["min_caller_edges"]
        file_suffix = spec.get("file")
        ids = dbmod.resolve_symbol(conn, name, file_suffix)
        if not ids:
            blocking.append(f"spot-check symbol '{name}' missing from index")
            continue
        total = sum(dbmod.caller_edge_count(conn, nid) for nid in ids)
        if total < min_callers:
            blocking.append(
                f"'{name}' has {total} caller edges (expected >= {min_callers}) "
                f"— index likely corrupt; run `codegraph index` (NOT sync)"
            )

    return blocking, warnings
