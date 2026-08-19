"""Direct read access to a CodeGraph SQLite index.

We own the traversal rather than shelling out to `codegraph impact`, because
that CLI returns only immediate dependents + a file-level cross-file bucket,
not a transitive symbol closure (verified on honeyslate 2026-07-17).
"""
import sqlite3

# Edge kinds that carry a behavior-reachability signal, walked in REVERSE
# (target -> source = "who depends on this").
#
# `imports` is included deliberately and is load-bearing for recall: honeyslate
# uses module-level singletons (`_settings = get_settings()`), which CodeGraph
# records ONLY as an `imports` edge (file -> symbol), never as a `calls` edge.
# Without `imports` the closure silently drops every journey that reads a shared
# global. `contains`/`instantiates`/`extends`/`references` round out the model.
REACH_KINDS = ("calls", "references", "instantiates", "extends", "imports")

# Confidence assigned to an edge whose metadata carries no `confidence` field
# (honeyslate: 61 `references` rows tagged {"valueRef":true}, plus one `calls`).
# Deliberately high: confidence only ANNOTATES a selection, never drops it, so
# guessing low on an unmeasurable edge would manufacture false "verify manually"
# noise rather than protect anything.
DEFAULT_EDGE_CONFIDENCE = 0.9

# `provenance='heuristic'` marks a synthesized edge (JSX render, dynamic
# dispatch) that no static resolution proved. Capped hard, whatever its metadata
# claims.
HEURISTIC_CONFIDENCE = 0.3

# At or below this, a journey is reported as needing manual verification. Splits
# the observed 0.5 edge tier from 0.7+.
LOW_CONFIDENCE = 0.6


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def schema_version(conn):
    """Return the stored schema version, or None. Callers fail loud on drift
    (the db schema is a codegraph-internal contract, not a public API)."""
    try:
        row = conn.execute(
            "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def nodes_for_lines(conn, file_path, lo, hi):
    """Symbol nodes in `file_path` whose [start_line, end_line] overlaps the
    changed hunk [lo, hi]."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM nodes WHERE file_path = ? AND kind != 'file' "
            "AND NOT (end_line < ? OR start_line > ?)",
            (file_path, lo, hi),
        )
    ]


def nodes_in_file(conn, file_path):
    """Every symbol node in `file_path`. Used to seed whole-file changes
    (deletions, renames) where there are no line ranges to map."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM nodes WHERE file_path = ? AND kind != 'file'",
            (file_path,),
        )
    ]


def file_node_id(conn, file_path):
    row = conn.execute(
        "SELECT id FROM nodes WHERE file_path = ? AND kind = 'file' LIMIT 1",
        (file_path,),
    ).fetchone()
    return row[0] if row else None


def closure_files(conn, node_ids):
    """Distinct `file_path` values for a set of node ids (file-kind nodes
    counted by their own path). Used to detect a closure that never leaves
    the file its seeds started in — an edge-resolution blind spot distinct
    from an unmapped seed (issue #63): the seeds resolved fine, they just
    have no outbound reach on record."""
    node_ids = list(node_ids)
    if not node_ids:
        return set()
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"SELECT DISTINCT file_path FROM nodes WHERE id IN ({placeholders})",
        node_ids,
    )
    return {r[0] for r in rows}


def impacted_closure(conn, seed_ids):
    """Transitive reverse-reachability closure of `seed_ids`, as
    `{node_id: confidence}`.

    Two propagation rules (validated on honeyslate):
      1. reverse over REACH_KINDS: callers/importers of an impacted node.
      2. when a FILE node enters the closure (a module-scope dependency), expand
         it to every symbol it `contains` — the whole module can be affected.

    Confidence is `max over paths of (min over edges on the path)`: a chain is
    only as trustworthy as its weakest hop, but one solid route is enough to
    trust the selection. Seeds start at 1.0. `contains` expansion inherits the
    file node's confidence unchanged — containment is a structural fact, not an
    inference hop.

    Terminates despite cycles: edge confidences come from a finite set and `min`
    is monotone, so the (id, conf) pair space is finite and UNION converges.
    """
    if not seed_ids:
        return {}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seeds(id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _seeds")
    conn.executemany(
        "INSERT OR IGNORE INTO _seeds(id) VALUES (?)", [(s,) for s in seed_ids]
    )
    kinds = ",".join("'%s'" % k for k in REACH_KINDS)  # constants, safe to inline
    edge_conf = (
        f"MIN(COALESCE(json_extract(e.metadata, '$.confidence'), "
        f"{DEFAULT_EDGE_CONFIDENCE}), "
        f"CASE WHEN e.provenance = 'heuristic' THEN {HEURISTIC_CONFIDENCE} "
        f"ELSE 1.0 END)"
    )
    query = f"""
    WITH RECURSIVE impacted(id, conf) AS (
        SELECT id, 1.0 FROM _seeds
        UNION
        SELECT e.source, MIN(i.conf, {edge_conf})
            FROM edges e JOIN impacted i ON e.target = i.id
            WHERE e.kind IN ({kinds})
        UNION
        SELECT e.target, i.conf FROM edges e JOIN impacted i ON e.source = i.id
            WHERE e.kind = 'contains' AND i.id LIKE 'file:%'
    )
    SELECT id, max(conf) FROM impacted GROUP BY id
    """
    return {r[0]: r[1] for r in conn.execute(query)}


def caller_edge_count(conn, node_id):
    """Direct inbound dependency edges — used by the integrity spot-check."""
    return conn.execute(
        "SELECT count(*) FROM edges WHERE target = ? "
        "AND kind IN ('calls', 'imports', 'references', 'instantiates')",
        (node_id,),
    ).fetchone()[0]


def top_fanin_nodes(conn, limit, exclude_source=None):
    """[(name, file_path, inbound_edge_count, [caller file, ...])] for the most
    depended-on symbols.

    Used by `propose` to pin integrity spot-checks from the index itself rather
    than from a hand-picked symbol. Same edge kinds as `caller_edge_count`.

    Caller file paths come out because the count alone is the wrong signal: the
    floor breaks when fan-in DROPS, fan-in drops when callers are deleted, and
    how volatile the calling files are is what predicts that (issue #43). They
    are already in this join, so returning them costs nothing.

    `exclude_source` is a predicate on the SOURCE node's file path. It exists
    because the floor derived here is later compared against `caller_edge_count`,
    which counts edges from EVERY file including tests: counting test call sites
    into the floor makes ordinary test deletion drop the measured count below it,
    and the guard then blocks with `run codegraph index`, a remedy that can never
    clear it. Excluding them here leaves the floor at or below what the guard
    measures, so the error can only be in the safe direction.
    """
    rows = conn.execute(
        "SELECT n.name, n.file_path, src.file_path FROM edges e "
        "JOIN nodes n ON n.id = e.target "
        "LEFT JOIN nodes src ON src.id = e.source "
        "WHERE e.kind IN ('calls', 'imports', 'references', 'instantiates') "
        "AND n.kind != 'file'"
    )
    counts, callers = {}, {}
    for name, file_path, source_path in rows:
        if exclude_source and source_path and exclude_source(source_path):
            continue
        key = (name, file_path)
        counts[key] = counts.get(key, 0) + 1
        if source_path:
            callers.setdefault(key, []).append(source_path)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
    return [
        (name, path, count, callers.get((name, path), []))
        for (name, path), count in ranked[:limit]
    ]


def resolve_symbol(conn, name, file_suffix=None):
    """Node ids for a symbol by name, optionally constrained to a file suffix
    (kills same-name duplicates like auth.me vs test.me)."""
    if file_suffix:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind != 'file' "
            "AND file_path LIKE ?",
            (name, f"%{file_suffix}"),
        )
    else:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind != 'file'", (name,)
        )
    return [r[0] for r in rows]
