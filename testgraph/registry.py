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


JOURNEYS_DIR = os.path.join(os.path.dirname(__file__), "..", "journeys")


def repo_name(repo):
    """The project name for a repo path, seeing through the bare-worktree layout.

    `~/personal_projects/signedintake/main` and `.../signedintake/claude-2026…`
    are both worktrees of *signedintake*; their basenames are `main` and
    `claude-2026…`. Taking the basename blind is how a caller ends up asking for
    a registry named `main`."""
    parts = [p for p in os.path.abspath(repo).split(os.sep) if p]
    if not parts:
        return ""
    leaf = parts[-1]
    if len(parts) > 1 and (leaf == "main" or leaf.startswith(("claude-", "codex-"))):
        return parts[-2]
    return leaf


def resolve_for_repo(repo, journeys_dir=None):
    """Path to the registry whose `target` is this repo, or None.

    Matched on the registry's self-declared `target`, not on filename: the file
    is an implementation detail, the target is the claim. Returns None rather
    than guessing — a WRONG registry is worse than no registry, because every
    downstream check then reports disagreement with the code as staleness. That
    was the real behaviour before this existed: `select --repo <signedintake>`
    silently loaded honeyslate's registry and blamed the index."""
    directory = journeys_dir or JOURNEYS_DIR
    name = repo_name(repo)
    if not name or not os.path.isdir(directory):
        return None
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(directory, fname)
        try:
            with open(path) as f:
                target = json.load(f).get("target")
        except (OSError, ValueError):
            continue
        if target == name:
            return path
    return None


def resolve_entries(conn, registry):
    """entry_node_id -> {journey_id}. Maps ALL nodes matching an entry (name +
    file) so no definition of a handler is missed, and ALL journeys claiming a
    node so no *journey* is missed either — both halves of recall-first.

    One symbol legitimately sits on two flows: signedintake's
    `simulatePaymentCompletion` is the hinge between staff creating a payment
    request (J5) and the customer completing it (J11). Keying one journey per
    node silently kept whichever came last in registry order and dropped the
    rest, which `unresolved()` cannot detect because it re-resolves each entry
    instead of reading this map — a journey whose entries were all shared with
    a later journey would disappear from every answer while the exported map's
    legend still advertised it."""
    mapping = {}
    for jid, journey in sorted(
        registry["journeys"].items(), key=lambda kv: journey_sort_key(kv[0])
    ):
        for entry in journey["entries"]:
            ids = dbmod.resolve_symbol(conn, entry["name"], entry.get("file"))
            for nid in ids:
                mapping.setdefault(nid, set()).add(jid)
    return mapping


def approval_warning(registry):
    """A warning string when this registry has not been human-approved, else None.

    `testgraph.propose` drafts a registry mechanically (issue #6). A draft is
    valid and runnable — that is the point — but it groups one journey per route
    handler, it has known blind spots, and nobody has read it. Running against one
    is fine; running against one *without knowing* is the silent-confidence
    failure this codebase keeps designing against, so it rides the same warning
    channel as `RECALL DEGRADED`: loud, never blocking.

    Blocking was rejected deliberately: it would make the proposer useless until a
    human edits JSON, which defeats the purpose of automating authoring.

    A MISSING `approved` key warns too. An unmarked registry is unknown
    provenance, not an approved one. This is safe from the always-on-warning
    failure `unchecked_entries` documents, because the remedy — add
    `"approved": true` — always clears it.
    """
    if registry.get("approved") is True:
        return None
    spots = registry.get("blind_spots") or []
    extra = f"; {len(spots)} declared blind spot(s)" if spots else ""
    if "approved" not in registry:
        return (
            "registry carries no `approved` marker — provenance unknown, so "
            "completeness is unverified; add \"approved\": true once a human has "
            "read it" + extra
        )
    return (
        "UNAPPROVED REGISTRY — this is a machine-drafted registry nobody has "
        "reviewed. Journeys are one-per-handler and entry coverage is unverified, "
        "so a NONE answer may mean 'not registered' rather than 'not affected'" +
        extra + ". Set \"approved\": true after review"
    )


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
    for jid, journey in sorted(
        registry["journeys"].items(), key=lambda kv: journey_sort_key(kv[0])
    ):
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
    for jid, journey in sorted(
        registry["journeys"].items(), key=lambda kv: journey_sort_key(kv[0])
    ):
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
    answer for the only language any journey has entries in today. Entries this
    cannot verify are NOT returned here — see `unchecked_entries()`, a separate
    channel, so an unverifiable entry never reads as an integrity failure.

    Reported, never blocking, with a remedy per reason (`REMEDIES`). The check is an
    approximation in one direction: an entry re-exported into its registry `file`
    rather than defined there would be a false positive, so imports count as
    definitions.
    """
    sources = python_sources(repo)
    drift = []
    for jid, journey in sorted(
        registry["journeys"].items(), key=lambda kv: journey_sort_key(kv[0])
    ):
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
            # anchored on a path separator: a bare endswith also matched
            # `.../xrouters/tasks.py` for the suffix `routers/tasks.py`
            suffix = rel.lstrip("/")
            candidates = [
                p for p in sources
                if p == suffix or p.endswith(os.sep + suffix)
            ]
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
    # JS/TS build output. Added for `propose`, which counts the files it cannot
    # parse as evidence for a blind spot: honeyslate's `.svelte-kit/` inflated
    # that count from 53 real frontend sources to 96, so the report overstated
    # how much of the app was invisible. Bundled output can never hold a registry
    # entry, so skipping it costs nothing on the drift path either. All dotted:
    # this list also feeds `live_drift`, and a plausible package name like
    # `coverage/` would make a real entry under it report "no file matching that
    # path in the tree" with a remedy that cannot be right.
    ".svelte-kit", ".next", ".nuxt", ".output",
    # A vendored dependency tree with no `pyvenv.cfg` beside it — `prune_dirs`
    # cannot detect those by marker, so the directory name is the only signal.
    "site-packages",
})


def prune_dirs(root, dirs):
    """Prune `dirs` in place for a source walk. Shared by every walker.

    A virtualenv is identified by its **`pyvenv.cfg` marker**, not by its name.
    `_SKIP_DIRS` lists `.venv` and `venv`, and coriolis-local keeps one at
    `backend/.uv/wsl-venv/`, which matches neither: the scan walked its
    site-packages and reported 126 third-party functions as excluded journey
    candidates, burying the real exclusions and taking 25s. Name-based skipping
    cannot be made complete — every project is free to name its venv anything —
    so the marker file is the check that actually holds.
    """
    dirs[:] = [
        d
        for d in dirs
        if d not in _SKIP_DIRS
        and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))
    ]


def python_sources(repo):
    """Every Python file under `repo` that is product source. Public because
    `propose` scans the same set — one definition of "what counts as source",
    including `_SKIP_DIRS`, so the drafter and the drift checker cannot disagree
    about which files exist."""
    found = []
    for root, dirs, files in os.walk(repo):
        prune_dirs(root, dirs)
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
            # Nothing inside a function body is importable, so a nested
            # `def create_task()` must not satisfy a module-level entry — the same
            # false negative as a function-local binding, one level narrower. Method
            # names still match: their FunctionDef sits in a ClassDef body, which
            # does not set in_function.
            if not in_function and child.name == name:
                return True
        elif isinstance(child, ast.ClassDef):
            if not in_function and child.name == name:
                return True
            if _scan(child, name, in_function):
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


def journey_sort_key(jid):
    """Order journey ids the way a reader expects: J2 before J10.

    Plain string sort puts J10-J13 between J1 and J2, which only shows up once a
    registry passes nine journeys — signedintake did, and the exported map read
    as shuffled. Split the id into (non-digit prefix, number) so digits compare
    numerically; ids with no trailing number keep their lexicographic place."""
    head = jid.rstrip("0123456789")
    tail = jid[len(head):]
    return (head, int(tail) if tail else -1, jid)
