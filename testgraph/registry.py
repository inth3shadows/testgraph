"""Journey registry: a hand-authored map of user journeys to their entry
symbols. Names drift, node ids don't — so we store names + file and resolve to
node ids at run time.
"""
import ast
import json
import os

from . import db as dbmod


def load(path):
    with open(path) as f:
        return json.load(f)


def resolve_entries(conn, registry):
    """entry_node_id -> journey_id. Maps ALL nodes matching an entry (name +
    file) so no definition of a handler is missed (recall-first)."""
    mapping = {}
    for jid, journey in registry["journeys"].items():
        for entry in journey["entries"]:
            ids = dbmod.resolve_symbol(conn, entry["name"], entry.get("file"))
            for nid in ids:
                mapping[nid] = jid
    return mapping


def unresolved(conn, registry):
    """[(journey_id, [unresolvable entry names])] for journeys with NO entry that
    resolves to a node in the index.

    A journey in this state can never be selected: `resolve_entries` yields
    nothing for it, so it silently disappears from every answer while the
    registry and the map legend still advertise it as covered. Rename a FastAPI
    handler without updating the registry and testgraph will report that no
    change can affect that journey. Callers must fail loud on a non-empty
    result — this is the registry-rot half of the drift problem (issue #19).
    """
    out = []
    for jid, journey in registry["journeys"].items():
        missing = [
            e["name"]
            for e in journey["entries"]
            if not dbmod.resolve_symbol(conn, e["name"], e.get("file"))
        ]
        if len(missing) == len(journey["entries"]):
            out.append((jid, missing))
    return out


def live_drift(repo, registry):
    """[(journey_id, entry_name, file, reason)] for entries the INDEX resolves but
    the source on disk does not define.

    `unresolved()` above compares the registry to the index; both can agree and
    both be wrong, because the index is a snapshot. Rename a handler and run
    `select` before re-indexing: the stale node still resolves, the journey is
    still selected, and every answer is about a symbol that no longer exists.
    Nothing else in the pipeline reads the source, so nothing else can catch it.
    This is issue #7's "validate on each run, live parse, never stale".

    A **live parse of the file**, not a RunEcho MCP call as the issue proposed:
    testgraph is a CLI with no MCP client, and Python's own `ast` gives the same
    answer for the only language any journey has entries in today. The trade is
    explicit — non-Python entries are returned as `unchecked` rather than silently
    passing, so the gap is visible instead of assumed away.

    Reported, never blocking. The check is an approximation in one direction: an
    entry re-exported into its registry `file` rather than defined there is a
    false positive, and blocking on those would break real runs to report a
    freshness problem whose real remedy is `codegraph index`.
    """
    sources = _python_sources(repo)
    drift = []
    for jid, journey in registry["journeys"].items():
        for entry in journey["entries"]:
            rel = entry.get("file")
            name = entry["name"]
            if not rel:
                continue
            if not rel.endswith(".py"):
                drift.append((jid, name, rel, "unchecked (no parser for this file type)"))
                continue
            # Registry `file` values are SUFFIXES — `resolve_symbol` matches them
            # with a LIKE, so `routers/tasks.py` means `backend/app/routers/tasks.py`
            # here. Joining them onto the repo root instead reported all 16 of
            # honeyslate's entries as "file is gone", which is how this was caught.
            candidates = [p for p in sources if p.endswith(rel)]
            if not candidates:
                drift.append((jid, name, rel, "no file matching that path in the tree"))
                continue
            failures = []
            for path in candidates:
                try:
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                except (OSError, SyntaxError) as exc:
                    failures.append(f"cannot parse ({exc.__class__.__name__})")
                    continue
                if _defines(tree, name):
                    failures = []
                    break  # any definition counts — same recall-first rule as resolve_entries
                failures.append("no definition of that name in the file")
            if failures:
                drift.append((jid, name, rel, failures[0]))
    return drift


# Directories that are never product source. `.venv`/`node_modules` also make the
# walk O(dependencies) instead of O(project).
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".codegraph",
    ".testgraph", ".mypy_cache", ".pytest_cache", "dist", "build",
})


def _python_sources(repo):
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                found.append(os.path.join(root, f))
    return found


def _defines(tree, name):
    """Does this module define `name` anywhere — including as a method, a
    decorated function, or a module-level binding?

    Walks the whole tree rather than the top level: honeyslate's entries include
    methods, and a top-level-only check would report every one of them as drift.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            target = getattr(node, "target", None)
            if isinstance(target, ast.Name) and target.id == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # a re-export IS how the symbol becomes available under this path
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[-1]) == name:
                    return True
    return False


def journey_name(registry, jid):
    return registry["journeys"][jid]["name"]
