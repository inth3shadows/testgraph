"""Phase 5: two-sided reconciliation of the static graph against reality.

Closes issue #12. Answers #66/#82 with a measurement instead of an assertion.

Every earlier phase trusted CodeGraph's edges. This one puts them on trial, and
the whole design is about which witness is allowed to say what:

    RUNTIME (a trace)      proves PRESENCE. It ran, so it is real.
                           It proves nothing about absence: an untested branch
                           and a nonexistent one look identical.
    SOURCE (a strict AST)  proves ABSENCE OF SUPPORT. No syntax in the caller
                           could denote that callee under any binding the
                           module's imports permit.
                           It proves nothing about presence: dynamic dispatch
                           is invisible to it (that is `harness/fixtures/
                           dyndemo`'s entire point).
    STATIC (CodeGraph)     proves nothing here. It is the thing under test.

So the two defects get two different burdens of proof:

  STATIC MISS   a symbol a journey's tests actually EXECUTED, sitting outside
                that journey's static footprint. Runtime presence is proof, so
                this finding is proof-grade: edit that symbol and the selector
                stays silent about a journey the change can break.

  PHANTOM EDGE  a static `calls` edge whose caller contains no syntax that
                could denote the callee, cross-checked against a run in which
                the two never co-occurred. Neither witness alone is enough.

Why not `harness/ast_oracle.py`, which §5.1 of the roadmap names for this job:
it matches calls by BARE NAME (`ast.Attribute.attr`), which is precisely the
resolver defect #66 reported. Pointed at `rows.setdefault(k, []).append(v)` it
fabricates the same `-> ledger.append` edge CodeGraph does. An oracle that
reproduces the bug can only confirm it. That oracle stays what it is — a
deliberately COARSE, over-approximating recall oracle, which is the safe bias
for its job — and this module carries a second, STRICT one, whose bias runs
the other way and is the safe bias for this one.

Non-goals, each deliberate:

  - **Repairing the graph.** Findings are reported, never suppressed inside
    `select`. Tuning an oracle until it agrees with the thing it audits
    destroys the only reason it was independent — `ast_oracle`'s own docstring
    makes this point and it applies with more force here, where the audit has
    teeth.
  - **Caller->callee tracing.** `tgtrace` records symbols per test, not call
    pairs, and adding frame introspection to a `PY_START` callback taxes the
    hot path of a whole-suite trace. Co-occurrence within one test is the
    proxy used instead; see `runtime_support` for the bias it carries.
  - **Anything but Python.** Non-Python edges classify UNKNOWN and are
    counted, never judged.
"""
import ast
import collections
import os

from . import db as dbmod
from . import registry as reg

# What the source oracle is willing to say about one static edge.
SUPPORTED = "supported"      # some syntax in the caller can denote the callee
UNSUPPORTED = "unsupported"  # none can, under any binding this module declares
UNKNOWN = "unknown"          # the oracle cannot read this caller at all

# Why an edge went unjudged. Measured on testgraph: all 13 of them are
# `file -> main`, CodeGraph's record of a module-level `sys.exit(main())`.
# That is not a question about a function body and never should be judged,
# so a single opaque "not judged" count would invite someone to go looking
# for an oracle gap that is not there.
NOT_A_FUNCTION = "unjudged-not-a-function"
UNREADABLE = "unjudged-unreadable"

# What the run is willing to say about one static edge.
OBSERVED = "observed"          # caller and callee both ran in some one test
NOT_OBSERVED = "not-observed"  # the caller ran; the callee never ran with it
UNTESTED = "untested"          # the caller never ran, so the run is silent

# A phantom needs the source oracle to refuse the edge. The runtime column then
# says how strong the finding is, and `UNTESTED` is kept as its own grade
# rather than folded in: "the suite disagrees" and "the suite never looked" are
# different claims, and a reader deciding whether to open an upstream bug needs
# to know which one they have.
PHANTOM_CONFIRMED = "phantom-confirmed"
PHANTOM_SOURCE_ONLY = "phantom-source-only"
ORACLE_GAP = "oracle-gap"


# --- resolving traced symbols onto index nodes -------------------------------

def path_matches(indexed, rel):
    """Whether an index `file_path` denotes the traced relative path `rel`.

    A suffix match on whole path COMPONENTS. The tolerance is needed because
    the trace's relpath is taken against the TRACED root while `file_path` is
    relative to the INDEXED root; the component boundary is needed because a
    bare `indexed.endswith(rel)` also matches `myapp/dyn.py` against a traced
    `app/dyn.py`, binding a symbol to a same-named node in an unrelated
    directory with no symptom.

    This is stricter than `pytest_adapter.resolve_traced`, which resolves the
    same symbols through a bare `LIKE '%<relpath>'`. The looser bar is fine
    there: attribution only decides which journey a test is credited to, and
    it reports its ambiguities. Here a mis-binding would manufacture a STATIC
    MISS — a claim that the selector is broken — so the bar is raised rather
    than shared."""
    return indexed == rel or indexed.endswith("/" + rel)


def file_map(conn):
    """{node_id: file_path} for the whole index, read once per connection.

    Not memoised in a module global: keyed on node id alone, a global returns
    one database's answer for another's identically-named node."""
    return {
        row[0]: (row[1] or "")
        for row in conn.execute("SELECT id, file_path FROM nodes")
    }


def resolve_traced(conn, symbols, files):
    """[(relpath, qualname)] -> ({node_id}, [unresolved]).

    Only ANONYMOUS frames are dropped — `<lambda>`, `<listcomp>`, `<genexpr>`
    — because the index has no node for them and counting one as a miss would
    inflate the number with something no edge kind can fix. A NAMED closure is
    kept, unlike in `harness/ground_truth.py`, which drops the whole
    `<locals>` chain: CodeGraph does index nested defs as ordinary nodes in
    their file (`export.py:git` lives inside `commit_stamp`), so dropping them
    left `runtime_support` structurally unable to observe an edge into one —
    every such edge read NOT_OBSERVED and, on the first run of this module,
    two real edges were reported as phantoms on the strength of it.

    A name that resolves only in a DIFFERENT file is reported unresolved, not
    accepted — "the graph has no node for this" (an indexing gap) and "the
    graph has a node and no path to it" (a traversal gap) are different
    defects with different fixes, and merging them reports one as the other."""
    resolved, unresolved = set(), []
    for rel, qualname in symbols:
        if "<" in qualname.split(".")[-1]:
            continue
        name = qualname.split(".")[-1]
        ids = dbmod.resolve_symbol(conn, name, os.path.basename(rel))
        hits = [i for i in ids if path_matches(files.get(i, ""), rel)]
        if hits:
            resolved.update(hits)
        else:
            unresolved.append((rel, qualname))
    return resolved, unresolved


def trace_nodes(conn, trace_tests, files=None):
    """tgtrace's `{nodeid: [[rel, qualname], ...]}` -> ({test: {node_id}},
    [unresolved]). One resolution pass, reused by both halves of the report."""
    files = files if files is not None else file_map(conn)
    per_test, unresolved = {}, []
    for test, symbols in trace_tests.items():
        ids, missing = resolve_traced(conn, [tuple(s) for s in symbols], files)
        per_test[test] = ids
        unresolved.extend(missing)
    return per_test, unresolved


def invert(per_test):
    """{node_id: {test, ...}} — which tests executed each symbol."""
    out = collections.defaultdict(set)
    for test, ids in per_test.items():
        for nid in ids:
            out[nid].add(test)
    return out


# --- STATIC MISS -------------------------------------------------------------

def static_misses(conn, registry, per_test, reach, contained_by, files=None):
    """Per journey: the symbols its own tests ran that its footprint excludes.

    A test is credited to journey J when it executed one of J's ENTRY symbols
    — the same rule `pytest_adapter.attribute` uses and for the same reason
    (crediting by footprint intersection made one CLI's 34 tests cover every
    journey in the repo, because every footprint contains the shared core).

    Journeys no test covers are kept and flagged. A journey the suite never
    exercised produced no evidence either way, and dropping it silently would
    let "not measured" read as "agrees"."""
    files = files if files is not None else file_map(conn)
    entries = collections.defaultdict(set)
    for nid, jids in reg.resolve_entries(conn, registry).items():
        for jid in jids:
            entries[jid].add(nid)

    rows = []
    for jid in sorted(registry.get("journeys", {}), key=reg.journey_sort_key):
        journey = registry["journeys"][jid]
        entry_ids = entries.get(jid, set())
        covering = [t for t, ids in per_test.items() if ids & entry_ids]
        traced = set().union(*(per_test[t] for t in covering)) if covering else set()
        fp = dbmod.footprint(entry_ids, reach, contained_by) if entry_ids else set()
        rows.append({
            "journey": jid,
            "name": journey.get("name", ""),
            "entries_resolved": len(entry_ids),
            "covering_tests": len(covering),
            "traced_nodes": len(traced),
            "footprint": len(fp),
            "misses": sorted(traced - fp),
        })
    return rows


# --- the strict source oracle ------------------------------------------------

class _Module:
    """One parsed Python file: its import bindings and its function defs.

    `star` records `from x import *`, which makes every unbound name in the
    module potentially legitimate. A module with one is not judged at all —
    every edge out of it classifies UNKNOWN — because the alternative is
    calling a real edge fabricated on the strength of an import we refused to
    follow."""

    def __init__(self, tree):
        self.bindings = collections.defaultdict(set)
        self.star = False
        self.functions = collections.defaultdict(list)
        self.by_line = {}
        self.module_level = set()
        self._index(tree)

    def _index(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    dotted = a.name if a.asname else a.name.split(".")[0]
                    self.bindings[local].add(("module", dotted))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    if a.name == "*":
                        self.star = True
                        continue
                    local = a.asname or a.name
                    # `from . import db` binds a MODULE; `from .ledger import
                    # append` binds a SYMBOL. Which one a given statement is
                    # cannot be known without importing, so both are recorded.
                    # That over-binds, which suppresses phantom findings rather
                    # than manufacturing them — the safe direction.
                    self.bindings[local].add(
                        ("module", f"{mod}.{a.name}" if mod else a.name)
                    )
                    self.bindings[local].add(("symbol", (mod, a.name)))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name].append(node)
                self.by_line[node.lineno] = node
                for dec in node.decorator_list:
                    self.by_line.setdefault(dec.lineno, node)
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module_level.add(top.name)
            elif isinstance(top, ast.Assign):
                for t in top.targets:
                    if isinstance(t, ast.Name):
                        self.module_level.add(t.id)

    def find(self, name, start_line=None):
        """The def(s) an index node names. `start_line` first — CodeGraph may
        record either the `def` line or the first decorator, so both are
        indexed — then every same-named def as a fallback. Several defs share
        a name legitimately (a method on two classes); support is the UNION
        over them, which again over-supports rather than over-accuses."""
        if start_line is not None and start_line in self.by_line:
            return [self.by_line[start_line]]
        return list(self.functions.get(name, ()))


def _parse(path, cache):
    if path in cache:
        return cache[path]
    try:
        with open(path, encoding="utf-8") as fh:
            module = _Module(ast.parse(fh.read()))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        module = None
    cache[path] = module
    return module


def _dotted(node):
    """`a.b.c` for a pure Name/Attribute chain, else None.

    None is the load-bearing answer. A receiver that is a call, a subscript, a
    literal or a comprehension — `rows.setdefault(k, []).append(v)`,
    `items[0].append(v)`, `[].append(v)` — cannot be resolved to a module by
    any amount of reading, and treating it as a callee reference is exactly
    the resolver defect this module exists to detect."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _module_matches(dotted, file_path):
    """Whether a dotted module path could denote `file_path`.

    Compared on the last segment, plus a containment check so a package import
    (`testgraph.db`) matches `testgraph/db.py` and a relative one (`from .
    import db`) matches it too. Coarse on purpose: a same-named module in
    another package reads as a match, which SUPPORTS an edge that may not be
    real. Over-supporting costs a missed phantom; under-supporting invents
    one, and this module's whole output is accusations."""
    segs = [s for s in dotted.split(".") if s]
    if not segs:
        return False
    stem = file_path[:-3] if file_path.endswith(".py") else file_path
    parts = stem.replace(os.sep, "/").split("/")
    return segs[-1] == parts[-1] or segs[-1] in parts


def _local_names(func):
    """Names bound inside `func` — parameters, assignments, `for` targets,
    `with ... as`, comprehension variables.

    Needed because a local can SHADOW a module-level function of the same
    name. `pytest_adapter.write_outcomes` takes an `append` parameter and
    calls `append(row)`; that call site denotes the parameter, not
    `ledger.append`. (The function is still supported — it names
    `ledger.append` two lines earlier — which is exactly why support is
    decided per FUNCTION and not per call site.)"""
    names = set()
    args = func.args
    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        names.update(a.arg for a in group)
    for extra in (args.vararg, args.kwarg):
        if extra:
            names.add(extra.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not func:
                names.add(node.name)
    return names


def _supports(func, module, caller_file, callee_name, callee_file):
    """Does anything in `func` denote `callee_name` defined in `callee_file`?

    Every reference is weighed, not only call positions: passing a function as
    a callback is a real dependency, and `append = append or ledger.append`
    is how a shadowed default gets its real value."""
    locals_ = _local_names(func)
    # A `def` NESTED inside the caller is a binding, not a shadow. CodeGraph
    # indexes it as an ordinary node in the caller's file (`export.py:git`,
    # defined inside `commit_stamp`; `select.py:_mark_unmapped`, defined inside
    # `select`), so an edge to it is real — and the first run of this oracle
    # called both fabricated, because `_local_names` collects nested def names
    # to catch the shadowing case and the bare-name branch then skipped them.
    # The two are told apart by file: a nested def in the caller's own file IS
    # the callee; a nested def with the callee's name in a DIFFERENT file
    # shadows it and is still no support.
    if caller_file == callee_file:
        for node in ast.walk(func):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == callee_name and node is not func):
                return True
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == callee_name:
            receiver = _dotted(node.value)
            if receiver is None:
                continue                    # the #66 shape — refuse to guess
            root = receiver.split(".")[0]
            if root in ("self", "cls"):
                return True                 # method dispatch; coarse, deliberate
            if root in locals_ and root not in module.bindings:
                continue                    # a local list, not a module
            candidates = {receiver}
            for kind, value in module.bindings.get(root, ()):
                if kind == "module":
                    candidates.add(
                        ".".join([value] + receiver.split(".")[1:])
                    )
            if any(_module_matches(c, callee_file) for c in candidates):
                return True
        elif isinstance(node, ast.Name) and node.id == callee_name:
            if callee_name in locals_:
                continue                    # shadowed by a parameter or a local
            for kind, value in module.bindings.get(callee_name, ()):
                if kind == "symbol":
                    mod, _orig = value
                    if not mod or _module_matches(mod, callee_file):
                        return True
            if callee_name in module.module_level and caller_file == callee_file:
                # A bare name resolving to a module-level def in the caller's
                # OWN file. Checked against the caller's path, not by matching
                # the callee's NAME against a module path — `append` is not a
                # module, so the name-based check silently rejected every
                # same-file call.
                return True
    return False


def source_support(repo_root, caller, callee, cache):
    """`(SUPPORTED | UNSUPPORTED | UNKNOWN, reason)` for one static edge.

    `reason` is set only alongside UNKNOWN, and names which kind of blindness
    it was — see NOT_A_FUNCTION / UNREADABLE.

    `caller` and `callee` are `{name, file_path, start_line, kind}` dicts."""
    ca, cb = caller.get("file_path", ""), callee.get("file_path", "")
    if not ca.endswith(".py") or not cb.endswith(".py"):
        return UNKNOWN, NOT_A_FUNCTION
    if caller.get("kind") not in ("function", "method"):
        return UNKNOWN, NOT_A_FUNCTION
    module = _parse(os.path.join(repo_root, ca), cache)
    if module is None or module.star:
        return UNKNOWN, UNREADABLE
    defs = module.find(caller["name"], caller.get("start_line"))
    if not defs:
        return UNKNOWN, UNREADABLE
    for func in defs:
        if _supports(func, module, ca, callee["name"], cb):
            return SUPPORTED, None
    # A caller in the callee's OWN file, referencing it by a bare name, is
    # handled above via `module_level`. Reaching here means no reference of
    # any shape denoted the callee.
    return UNSUPPORTED, None


# --- runtime cross-check -----------------------------------------------------

def runtime_support(node_tests, caller_id, callee_id):
    """OBSERVED / NOT_OBSERVED / UNTESTED for one static edge.

    Co-occurrence within a single test body stands in for an actual call pair,
    because `tgtrace` records symbols per test and not caller->callee edges.
    The proxy OVERSTATES support — a test that exercises both ends
    independently reads as OBSERVED — and that is the bias to want here: it
    makes a phantom HARDER to declare, never easier. A caller that never ran
    yields UNTESTED, kept distinct so a source-only finding is never quoted as
    if the suite had contradicted the edge."""
    ran = node_tests.get(caller_id, set())
    if not ran:
        return UNTESTED
    return OBSERVED if ran & node_tests.get(callee_id, set()) else NOT_OBSERVED


def classify(source, runtime):
    """The verdict table. Only the source oracle can open an accusation; the
    run decides how strong it is, or withdraws it."""
    if source == SUPPORTED or source == UNKNOWN:
        return None
    if runtime == OBSERVED:
        # The edge executes. The oracle was wrong — record that as its own
        # count rather than laundering oracle error into the phantom number.
        return ORACLE_GAP
    return PHANTOM_CONFIRMED if runtime == NOT_OBSERVED else PHANTOM_SOURCE_ONLY


# --- PHANTOM EDGE ------------------------------------------------------------

def phantom_edges(conn, repo_root, registry, node_tests, reach, contained_by):
    """Every selection-relevant `calls` edge, classified.

    Scoped to edges whose SOURCE sits in some journey footprint. A footprint
    grows forward from the entries, so an edge only changes an answer when its
    caller is already inside one — a fabricated edge between two symbols no
    journey reaches is a CodeGraph bug with no product consequence, and mixing
    the two would bury the findings that matter."""
    nodes = {
        row[0]: {"name": row[1], "file_path": row[2] or "",
                 "start_line": row[3], "kind": row[4]}
        for row in conn.execute(
            "SELECT id, name, file_path, start_line, kind FROM nodes"
        )
    }
    entries = collections.defaultdict(set)
    for nid, jids in reg.resolve_entries(conn, registry).items():
        for jid in jids:
            entries[jid].add(nid)
    footprints = {
        jid: dbmod.footprint(ids, reach, contained_by)
        for jid, ids in entries.items()
    }
    in_scope = set().union(*footprints.values()) if footprints else set()

    cache = {}
    findings, counts = [], collections.Counter()
    for source, target, line in conn.execute(
        "SELECT source, target, line FROM edges WHERE kind = 'calls'"
    ):
        if source not in in_scope or source not in nodes or target not in nodes:
            continue
        counts["scanned"] += 1
        support, why = source_support(
            repo_root, nodes[source], nodes[target], cache
        )
        counts[support] += 1
        if why:
            counts[why] += 1
        runtime = runtime_support(node_tests, source, target)
        verdict = classify(support, runtime)
        if verdict is None:
            continue
        counts[verdict] += 1
        findings.append({
            "verdict": verdict,
            "source": source,
            "target": target,
            "caller": f"{nodes[source]['name']} ({nodes[source]['file_path']})",
            "callee": f"{nodes[target]['name']} ({nodes[target]['file_path']})",
            "line": line,
            "runtime": runtime,
        })

    phantoms = [f for f in findings if f["verdict"] != ORACLE_GAP]
    _attach_selection_impact(findings, phantoms, entries, footprints, conn)
    return findings, counts, footprints


def _attach_selection_impact(findings, phantoms, entries, footprints, conn):
    """For each phantom, which journeys lose the callee once it is removed.

    Every phantom is dropped AT ONCE and each footprint recomputed, rather
    than one edge at a time. Two phantoms reaching the same callee would each
    look harmless alone — the other still carries it in — and reporting that
    as "no selection impact" is the arithmetic that lets a fabricated
    dependency hide behind its twin.

    This is the line that turns "CodeGraph has a bug" into a product claim:
    J4 selecting on `ledger.append` through a dependency that does not exist,
    while J1 — which really does write the ledger — does not select on it at
    all."""
    if not phantoms:
        return
    drop = {(f["source"], f["target"]) for f in phantoms}
    clean_reach, clean_contained = dbmod.dependency_graph(conn, drop_edges=drop)
    clean = {
        jid: dbmod.footprint(ids, clean_reach, clean_contained)
        for jid, ids in entries.items()
    }
    for f in phantoms:
        lost = sorted(
            jid for jid, fp in footprints.items()
            if f["target"] in fp and f["target"] not in clean.get(jid, set())
        )
        f["journeys_losing_callee"] = sorted(lost, key=reg.journey_sort_key)


# --- top level ---------------------------------------------------------------

def reconcile(db_path, repo_root, registry, trace):
    """The whole report, as data. `trace` is a parsed tgtrace payload."""
    conn = dbmod.connect(db_path)
    files = file_map(conn)
    per_test, unresolved = trace_nodes(conn, trace.get("tests", {}), files)
    node_tests = invert(per_test)
    reach, contained_by = dbmod.dependency_graph(conn)

    misses = static_misses(conn, registry, per_test, reach, contained_by, files)
    findings, counts, footprints = phantom_edges(
        conn, repo_root, registry, node_tests, reach, contained_by
    )
    # Only the nodes the report names. Labelling the whole index would put
    # every symbol in the repo into `--json` output that mentions a handful.
    named = {nid for row in misses for nid in row["misses"]}
    labels = {
        nid: f"{name} ({path or ''})"
        for nid, name, path in conn.execute("SELECT id, name, file_path FROM nodes")
        if nid in named
    }
    return {
        "tests_traced": len(per_test),
        "unresolved_symbols": sorted(set(unresolved)),
        "static_misses": misses,
        "edges": findings,
        "edge_counts": dict(counts),
        "labels": labels,
    }


def render(report, max_examples=8):
    """Human-readable. The headline refuses to print a number when nothing was
    measured: "0 static misses" out of a run that traced no test is the
    sentence a reader quotes, and it is indistinguishable from a clean bill
    (the same refusal `harness/ground_truth.py` makes, and the same one issue
    #75 fixed for a NONE printed over a trust warning)."""
    lines = []
    scored = [r for r in report["static_misses"]
              if r["covering_tests"] and r["entries_resolved"]]
    if not report["tests_traced"] or not scored:
        lines.append(
            f"NO MEASUREMENT — {report['tests_traced']} test(s) traced, "
            f"{len(scored)} journey(s) both covered and resolvable. Nothing was "
            f"compared, so there is no miss count and no phantom count."
        )
        return "\n".join(lines)

    total_misses = sum(len(r["misses"]) for r in scored)
    counts = report["edge_counts"]
    lines.append(
        f"STATIC MISS   {total_misses} symbol(s) executed outside their "
        f"journey's footprint, across {len(scored)} scored journey(s)"
    )
    lines.append(
        f"PHANTOM EDGE  {counts.get(PHANTOM_CONFIRMED, 0)} confirmed, "
        f"{counts.get(PHANTOM_SOURCE_ONLY, 0)} source-only, of "
        f"{counts.get('scanned', 0)} selection-relevant calls edge(s) "
        f"[{counts.get(NOT_A_FUNCTION, 0)} not a function body, "
        f"{counts.get(UNREADABLE, 0)} unreadable, "
        f"{counts.get(ORACLE_GAP, 0)} oracle gap]"
    )
    lines.append("")

    for r in report["static_misses"]:
        if not r["entries_resolved"]:
            lines.append(
                f"  {r['journey']}  {r['name']}  — ENTRIES UNRESOLVED (its "
                f"footprint is empty for that reason, not because the selector "
                f"missed anything; check the registry against this --db)"
            )
            continue
        if not r["covering_tests"]:
            lines.append(
                f"  {r['journey']}  {r['name']}  — NO COVERING TEST (the suite "
                f"never ran its entries; not evidence of agreement)"
            )
            continue
        flag = "  ! STATIC MISS" if r["misses"] else ""
        lines.append(
            f"  {r['journey']}  {r['name']}{flag}\n"
            f"      {r['covering_tests']} covering test(s) ran "
            f"{r['traced_nodes']} node(s); footprint {r['footprint']}; "
            f"{len(r['misses'])} outside it"
        )
        for nid in r["misses"][:max_examples]:
            lines.append(f"        - {report['labels'].get(nid, nid)}")
        if len(r["misses"]) > max_examples:
            lines.append(f"        … {len(r['misses']) - max_examples} more")

    phantoms = [e for e in report["edges"] if e["verdict"] != ORACLE_GAP]
    if phantoms:
        lines.append("")
        for e in phantoms:
            grade = "confirmed" if e["verdict"] == PHANTOM_CONFIRMED else "source-only"
            lost = e.get("journeys_losing_callee") or []
            lines.append(
                f"  ! PHANTOM EDGE ({grade}, runtime {e['runtime']})\n"
                f"      {e['caller']}:{e['line']} -> {e['callee']}\n"
                f"      selection impact: "
                + (
                    f"{', '.join(lost)} "
                    f"{'depends' if len(lost) == 1 else 'depend'} on the callee "
                    f"ONLY through this edge and its twins"
                    if lost else "none — the callee is reachable another way"
                )
            )
    gaps = [e for e in report["edges"] if e["verdict"] == ORACLE_GAP]
    if gaps:
        lines.append("")
        lines.append(
            f"  {len(gaps)} ORACLE GAP — edge(s) the source oracle could not "
            f"support but the run executed anyway. Not phantoms; the oracle's "
            f"own error rate, reported so it cannot be read as one."
        )
        for e in gaps[:max_examples]:
            lines.append(f"      {e['caller']}:{e['line']} -> {e['callee']}")
    if report["unresolved_symbols"]:
        lines.append("")
        lines.append(
            f"  {len(report['unresolved_symbols'])} traced symbol(s) have no "
            f"node in the index — an indexing gap, kept out of the miss count "
            f"because it needs a different fix than a missing edge"
        )
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import json

    ap = argparse.ArgumentParser(
        prog="testgraph.reconcile",
        description="classify static-graph vs runtime disagreements "
                    "(STATIC MISS / PHANTOM EDGE)",
        epilog="Produce --trace with:\n"
               "  python3 -m testgraph.pytest_adapter --repo . --dry-run "
               "--trace-out /tmp/t.json -- -q",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument("--trace", required=True, help="a tgtrace JSON payload")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--db", default=None,
                    help="codegraph db (default: <repo>/.codegraph/codegraph.db)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    registry_path = args.registry or reg.resolve_for_repo(args.repo)
    if registry_path is None:
        print(f"no journey registry for {reg.repo_name(args.repo)} — looked in "
              f"{reg.where_it_looked(args.repo)}")
        return 2
    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    if not os.path.isfile(db_path):
        print(f"no codegraph index at {db_path} — run `codegraph init` in {args.repo}")
        return 2
    try:
        registry = reg.load(registry_path)
        with open(args.trace) as fh:
            trace = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot read {registry_path} / {args.trace}: {exc}")
        return 2

    report = reconcile(db_path, args.repo, registry, trace)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True, default=sorted))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
