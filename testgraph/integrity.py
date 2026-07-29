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
import os

from . import db as dbmod


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
    #    — a slightly stale index degrades precision, not recall, and codegraph
    #    git hooks normally keep it synced.
    stale = []
    for row in conn.execute(
        "SELECT path, indexed_at FROM files WHERE language = 'python'"
    ):
        p = os.path.join(repo_root, row["path"])
        idx = row["indexed_at"]
        if not idx or not os.path.exists(p):
            continue
        idx_s = idx / 1000.0 if idx > 1e12 else idx  # normalize ms -> s
        if os.path.getmtime(p) > idx_s + 2:
            stale.append(row["path"])
    if stale:
        warnings.append(
            f"{len(stale)} source file(s) newer than the index "
            f"(e.g. {stale[0]}) — consider `codegraph sync`"
        )

    # 3. caller-count spot-check — the corruption detector `sync` can't clear.
    for name, spec in spot_checks.items():
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
