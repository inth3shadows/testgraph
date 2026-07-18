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


def file_node_id(conn, file_path):
    row = conn.execute(
        "SELECT id FROM nodes WHERE file_path = ? AND kind = 'file' LIMIT 1",
        (file_path,),
    ).fetchone()
    return row[0] if row else None


def impacted_closure(conn, seed_ids):
    """Transitive reverse-reachability closure of `seed_ids`.

    Two propagation rules (validated on honeyslate):
      1. reverse over REACH_KINDS: callers/importers of an impacted node.
      2. when a FILE node enters the closure (a module-scope dependency), expand
         it to every symbol it `contains` — the whole module can be affected.
    """
    if not seed_ids:
        return set()
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seeds(id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _seeds")
    conn.executemany(
        "INSERT OR IGNORE INTO _seeds(id) VALUES (?)", [(s,) for s in seed_ids]
    )
    kinds = ",".join("'%s'" % k for k in REACH_KINDS)  # constants, safe to inline
    query = f"""
    WITH RECURSIVE impacted(id) AS (
        SELECT id FROM _seeds
        UNION
        SELECT e.source FROM edges e JOIN impacted i ON e.target = i.id
            WHERE e.kind IN ({kinds})
        UNION
        SELECT e.target FROM edges e JOIN impacted i ON e.source = i.id
            WHERE e.kind = 'contains' AND i.id LIKE 'file:%'
    )
    SELECT id FROM impacted
    """
    return {r[0] for r in conn.execute(query)}


def caller_edge_count(conn, node_id):
    """Direct inbound dependency edges — used by the integrity spot-check."""
    return conn.execute(
        "SELECT count(*) FROM edges WHERE target = ? "
        "AND kind IN ('calls', 'imports', 'references', 'instantiates')",
        (node_id,),
    ).fetchone()[0]


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
