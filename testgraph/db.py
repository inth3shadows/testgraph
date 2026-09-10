"""Direct read access to a CodeGraph SQLite index.

We own the traversal rather than shelling out to `codegraph impact`, because
that CLI returns only immediate dependents + a file-level cross-file bucket,
not a transitive symbol closure (verified on honeyslate 2026-07-17).
"""
import collections
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

# A STATICALLY EXTRACTED edge can be just as wrong as a synthesized one, and
# nothing in `provenance` says so -- it is NULL for all of them.
#
# CodeGraph records HOW it resolved each edge in `metadata.resolvedBy`.
# `import` / `qualified-name` are backed by evidence in the source. `exact-match`
# means it matched a bare NAME, and when several symbols share that name it is a
# guess wearing the same confidence as a proof. Measured on the codegraph repo's
# own index: 16,569 of 29,440 `calls` edges are `exact-match`, and 8,407 of those
# point at a name more than one symbol has.
#
# The live failure this exists for (codegraph #66): with a module-level `append`
# in the project, `rows["k"].append(2)` extracted as a bare `append` and resolved
# onto it --
#     {"confidence":0.9,"resolvedBy":"exact-match","refName":"append"}
# -- an edge that does not exist, arriving in testgraph's TOP trust tier.
#
# Capped only when the name is genuinely AMBIGUOUS. A single-candidate
# exact-match had nothing to get wrong, and capping it would flood
# `verify_manually` (56% of call edges) until the flag meant nothing.
AMBIGUOUS_NAME_MATCH_CONFIDENCE = 0.5

# Resolvers whose edge rests on a bare name rather than on evidence in the
# source. A resolver we do not know is NOT treated as suspect: an unrecognized
# (or missing) `resolvedBy` leaves the edge scored exactly as before, so a
# codegraph change can cost us the protection but can never invent a flag.
NAME_ONLY_RESOLVERS = ("exact-match",)

# At or below this, a journey is reported as needing manual verification. Splits
# the observed 0.5 edge tier from 0.7+.
LOW_CONFIDENCE = 0.6


# The cap that held a route down, as the token the closure carries and the
# phrase a reader sees. The token is produced by the SQL, not inferred from the
# resulting number.
#
# Inferring it from the number was wrong and shipped wrong: `0.5` is not a value
# only the name-collision cap can produce -- it is a tier real indexes report
# directly (`LOW_CONFIDENCE`'s own comment says so). On honeyslate all 37 edges
# at 0.5 have unique names, so every "name-collision" label the value-based
# version produced was false, and it reproduced on this repo's own fixture,
# where `mid_a` carries a plain `{"confidence":0.5}` and no `resolvedBy` at all.
CAP_REASONS = {
    "heuristic": "synthesized edge (no static proof)",
    "name-collision": "name-collision edge (matched a name several symbols share)",
    "metadata": "low-confidence edge path",
}


def weak_edge_reason(cap):
    """The phrase for a cap token from `impacted_closure(..., with_reasons=True)`,
    or None when nothing capped the route (`''`) or the token is unknown."""
    return CAP_REASONS.get(cap)


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


def extraction_version(conn):
    """The extractor version that BUILT this index, or None.

    `project_metadata.indexed_with_extraction_version` — written by codegraph
    at index time, and the only field that distinguishes an index whose edges
    came from a superseded extractor. The release version does NOT: measured
    on this repo, a synced index and a freshly built one of the same commit
    both reported `indexed_with_version` 1.6.0 while their extraction versions
    were 25 and 26, and the 25 index still carried seven fabricated edges a
    fix had already removed (issue #85).

    None on an older codegraph that never wrote the row — indistinguishable
    from a codegraph that wrote it and shouldn't have, so callers warn rather
    than block."""
    try:
        row = conn.execute(
            "SELECT value FROM project_metadata "
            "WHERE key = 'indexed_with_extraction_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
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


def _load_id_temp_table(conn, table_name, ids):
    """(Re)fill a single-column TEMP TABLE with `ids`, for callers that need
    to join or filter against a large id set. A raw `IN (?,?,...)` with one
    bound placeholder per id hits SQLite's bound-parameter ceiling (999 on
    some packaged builds) once a closure fans out wide enough; a temp table
    has no such limit. Shared by `impacted_closure` and `closure_files` so
    that fix applies once, not per copy.
    """
    conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {table_name}(id TEXT PRIMARY KEY)")
    conn.execute(f"DELETE FROM {table_name}")
    conn.executemany(
        f"INSERT OR IGNORE INTO {table_name}(id) VALUES (?)", [(i,) for i in ids]
    )


def closure_files(conn, node_ids):
    """Distinct `file_path` values for a set of node ids (file-kind nodes
    counted by their own path). Used to detect a closure that never leaves
    the file its seeds started in — an edge-resolution blind spot distinct
    from an unmapped seed (issue #63): the seeds resolved fine, they just
    have no outbound reach on record.

    Returns `None`, not a smaller set, if any id has no matching `nodes` row.
    `edges` can name an id `nodes` has no row for (a dangling edge — the same
    kind of drift `integrity.content_drift` exists elsewhere to catch); a
    plain `WHERE id IN (...)` join silently drops such an id, which would
    otherwise read identically to "this id resolves to no other file" and
    manufacture a false confinement signal out of an untrustworthy index.
    """
    node_ids = list(node_ids)
    if not node_ids:
        return set()
    _load_id_temp_table(conn, "_closure_ids", node_ids)
    rows = list(conn.execute(
        "SELECT id, file_path FROM nodes WHERE id IN (SELECT id FROM _closure_ids)"
    ))
    if len({r[0] for r in rows}) != len(set(node_ids)):
        return None
    return {r[1] for r in rows}


def _load_ambiguous_names_table(conn):
    """Ensure `_ambiguous_ids` holds every non-file node whose `name` is shared
    by at least one other non-file node. Built ONCE per connection.

    Membership is the discriminator for `AMBIGUOUS_NAME_MATCH_CONFIDENCE`: what
    separates "matched the only symbol with this name" from "picked one of
    several".

    Rebuilding it per call was a real cost, not a theoretical one: it is an
    index-wide `GROUP BY name` (~80 ms on a 17k-node index), and `export.build_map`
    calls `impacted_closure` once per node -- so a per-call rebuild is quadratic
    and measured 2.4-2.7x on the map builds, projecting to ~21 minutes of
    identical redundant work on a large index. TEMP tables are per-connection, so
    its presence is exactly the right cache key and needs no bookkeeping.

    A connection held open across a re-index would see stale ambiguity data.
    That can only mislabel or misgrade a cap, never change closure MEMBERSHIP,
    and no caller here re-indexes mid-connection; `refresh_ambiguous_names` is
    the escape hatch if one ever does.
    """
    exists = conn.execute(
        "SELECT 1 FROM temp.sqlite_master WHERE type = 'table' "
        "AND name = '_ambiguous_ids'"
    ).fetchone()
    if exists:
        return
    conn.execute("CREATE TEMP TABLE _ambiguous_ids(id TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT OR IGNORE INTO _ambiguous_ids(id) "
        "SELECT id FROM nodes WHERE kind != 'file' AND name IN ("
        "  SELECT name FROM nodes WHERE kind != 'file' "
        "  GROUP BY name HAVING count(*) > 1)"
    )


def refresh_ambiguous_names(conn):
    """Drop the per-connection ambiguity cache so the next closure rebuilds it.
    Only needed if a connection outlives a re-index."""
    conn.execute("DROP TABLE IF EXISTS temp._ambiguous_ids")


def impacted_closure(conn, seed_ids, with_reasons=False):
    """Transitive reverse-reachability closure of `seed_ids`, as
    `{node_id: confidence}` — or `({node_id: confidence}, {node_id: cap_token})`
    when `with_reasons`.

    Two propagation rules (validated on honeyslate):
      1. reverse over REACH_KINDS: callers/importers of an impacted node.
      2. when a FILE node enters the closure (a module-scope dependency), expand
         it to every symbol it `contains` — the whole module can be affected.

    Confidence is `max over paths of (min over edges on the path)`: a chain is
    only as trustworthy as its weakest hop, but one solid route is enough to
    trust the selection. Seeds start at 1.0. `contains` expansion inherits the
    file node's confidence unchanged — containment is a structural fact, not an
    inference hop.

    Three caps compose inside that per-edge `min`: the metadata confidence,
    `provenance='heuristic'` (synthesized), and name-only resolution onto an
    ambiguous name (fabricated — see `AMBIGUOUS_NAME_MATCH_CONFIDENCE`). None of
    them drops an edge; they only lower what the selection claims about it.

    The cap token rides ALONG the walk rather than being read back off the
    number, because confidence values are not unique to a cap — `0.5` is a tier
    real indexes report directly. Each hop keeps the token of whichever cap
    produced the value that survived its `min`, and a tie goes to the edge (the
    more specific fact) rather than to the route so far.

    Terminates despite cycles: edge confidences come from a finite set, `min` is
    monotone, and the token is drawn from a four-element set, so the
    (id, conf, cap) triple space is finite and UNION converges.
    """
    if not seed_ids:
        return ({}, {}) if with_reasons else {}
    _load_id_temp_table(conn, "_seeds", seed_ids)
    _load_ambiguous_names_table(conn)
    kinds = ",".join("'%s'" % k for k in REACH_KINDS)  # constants, safe to inline
    resolvers = ",".join("'%s'" % r for r in NAME_ONLY_RESOLVERS)  # constants

    # The three caps, as separate expressions so the walk can say which one won.
    meta_conf = (
        f"COALESCE(json_extract(e.metadata, '$.confidence'), "
        f"{DEFAULT_EDGE_CONFIDENCE})"
    )
    heuristic_conf = (
        f"CASE WHEN e.provenance = 'heuristic' THEN {HEURISTIC_CONFIDENCE} "
        f"ELSE 1.0 END"
    )
    # Name-only resolution onto a name several symbols share.
    collision_conf = (
        f"CASE WHEN json_extract(e.metadata, '$.resolvedBy') IN ({resolvers}) "
        f"AND e.target IN (SELECT id FROM _ambiguous_ids) "
        f"THEN {AMBIGUOUS_NAME_MATCH_CONFIDENCE} ELSE 1.0 END"
    )
    # Composed, so an edge that is BOTH heuristic and a name collision keeps the
    # lower of the two rather than whichever cap is checked last.
    edge_conf = f"MIN({meta_conf}, {heuristic_conf}, {collision_conf})"
    edge_cap = (
        f"CASE WHEN {collision_conf} <= {meta_conf} "
        f"AND {collision_conf} <= {heuristic_conf} THEN 'name-collision' "
        f"WHEN {heuristic_conf} <= {meta_conf} THEN 'heuristic' "
        f"WHEN {meta_conf} <= {LOW_CONFIDENCE} THEN 'metadata' ELSE '' END"
    )
    query = f"""
    WITH RECURSIVE impacted(id, conf, cap) AS (
        SELECT id, 1.0, '' FROM _seeds
        UNION
        SELECT e.source, MIN(i.conf, {edge_conf}),
            CASE WHEN {edge_conf} > i.conf THEN i.cap ELSE {edge_cap} END
            FROM edges e JOIN impacted i ON e.target = i.id
            WHERE e.kind IN ({kinds})
        UNION
        SELECT e.target, i.conf, i.cap FROM edges e JOIN impacted i ON e.source = i.id
            WHERE e.kind = 'contains' AND i.id LIKE 'file:%'
    )
    -- `cap` is a bare column beside max(): SQLite documents that it takes its
    -- value from the row the max came from, which is exactly the surviving
    -- route's cap.
    SELECT id, max(conf), cap FROM impacted GROUP BY id
    """
    rows = list(conn.execute(query))
    conf = {r[0]: r[1] for r in rows}
    if not with_reasons:
        return conf
    return conf, {r[0]: r[2] for r in rows}


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


def dependency_graph(conn, drop_edges=()):
    """`(reach, contained_by)` — the adjacency `footprint` walks.

    The INVERSE derivation of `impacted_closure`'s two propagation rules, so
    `footprint(E)` is exactly the set whose members would put `E` in the
    impacted closure:

        impacted:  t in I, edge(s -> t, REACH_KIND)      => s in I
                   f in I, f is a file, contains(f -> x) => x in I

        inverted:  from n, add every t with edge(n -> t, REACH_KIND)
                   from n, add the file f with contains(f -> n)

    Only FILE containment inverts. A class containing a method is a `contains`
    edge too, and walking it would drag a whole class into every footprint by
    structure alone.

    `drop_edges` is an iterable of `(source, target)` pairs to omit. It exists
    for `testgraph.reconcile`, which answers "would this journey still depend
    on that symbol if the fabricated edges were not there" by building the
    graph twice and diffing. Rebuilding without them is the only honest way to
    ask: subtracting a node after the fact cannot tell whether some OTHER path
    still reaches it.

    Lifted here from `harness/couple.py` (which now delegates) because
    `reconcile` ships in the wheel and `harness/` does not.
    """
    drop = {tuple(pair) for pair in drop_edges}
    reach = collections.defaultdict(set)
    kinds = ",".join("'%s'" % k for k in REACH_KINDS)  # constants, safe to inline
    for source, target in conn.execute(
        f"SELECT source, target FROM edges WHERE kind IN ({kinds})"
    ):
        if (source, target) in drop:
            continue
        reach[source].add(target)

    contained_by = {}
    for source, target in conn.execute(
        "SELECT source, target FROM edges WHERE kind = 'contains'"
    ):
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
