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


# Remedy per reason. One hard-coded "run `codegraph index`" was wrong for most of
# these: `live_drift` reads the WORKING TREE while the rest of `select` reads
# committed history, so an in-progress uncommitted rename — the common case — would
# send an agent off to spend minutes re-indexing something that cannot change the
# answer. A registry typo or a syntax error is not an index problem at all.
REMEDIES = {
    "no definition of that name in the file":
        "the source and the index disagree — if the rename is committed, run "
        "`codegraph index`; if it is still uncommitted, commit first (select reads "
        "committed history); if the registry is simply wrong, fix the entry",
    "no file matching that path in the tree":
        "fix the registry entry's `file`, or restore the file",
    "cannot parse":
        "the file does not parse, so its symbols cannot be confirmed — fix the "
        "syntax error",
}


def remedy_for(reason):
    for key, text in REMEDIES.items():
        if reason.startswith(key):
            return text
    return "verify the registry entry against the source by hand"


def unchecked_entries(registry):
    """[(journey_id, entry_name, file)] for entries `live_drift` cannot verify.

    Kept OFF the integrity-warning channel deliberately. Emitting these as drift
    put "The index was not fully trustworthy" in every exported map the moment a
    non-Python entry existed, and told the reader to fix it by re-indexing — which
    can never clear an `unchecked` row. A banner that is always on, with a remedy
    that cannot work, is the exact failure `_map_relevant`'s docstring warns about.
    """
    out = []
    for jid, journey in registry["journeys"].items():
        for entry in journey["entries"]:
            rel = entry.get("file")
            if rel and not rel.endswith(".py"):
                out.append((jid, entry["name"], rel))
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
                continue  # reported by unchecked_entries(), a separate channel
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
                    # bytes, not text: a PEP-263 `# -*- coding: latin-1 -*-` file
                    # raised UnicodeDecodeError, which is a ValueError and was
                    # caught by neither clause — turning "reported, never
                    # blocking" into a traceback in both select and export.
                    # `ast.parse` honours the coding cookie itself.
                    with open(path, "rb") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                # ValueError is deliberate but NOT exercised on this
                # interpreter: CPython <= 3.11 raises it for NUL bytes in source
                # where 3.14 raises SyntaxError. A mutation removing it survives
                # the suite here, and that is reported rather than papered over
                # with a test that cannot fail on any version.
                except (OSError, SyntaxError, ValueError) as exc:
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
    """Does this module define `name` at module or class scope?

    Walks statements rather than `ast.walk`ing everything, because a
    function-LOCAL binding must not count: `def f(): create_task = build()` made
    `_defines(tree, "create_task")` true, so renaming the real module-level handler
    reported no drift — a false negative in the one direction this check exists to
    cover. Function and class *names* still match at any depth: honeyslate anchors
    J8 on the method `sweep`, and a top-level-only check would call it drift.
    """
    return _scan(tree, name, in_function=False)


def _scan(node, name, in_function):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name == name or _scan(child, name, True):
                return True
        elif isinstance(child, ast.ClassDef):
            if child.name == name or _scan(child, name, in_function):
                return True
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            # a re-export IS how the symbol becomes available under this path
            for alias in child.names:
                if (alias.asname or alias.name.split(".")[-1]) == name:
                    return True
        elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            if not in_function and _binds(child, name):
                return True
        elif _scan(child, name, in_function):
            # `if TYPE_CHECKING:` / `try: import ... except ImportError:` blocks
            # define real module-level names; descending keeps them visible.
            return True
    return False


def _binds(node, name):
    targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
    return any(isinstance(t, ast.Name) and t.id == name for t in targets if t)


def journey_name(registry, jid):
    return registry["journeys"][jid]["name"]
