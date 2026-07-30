"""testgraph propose — draft a journey registry for a repo that has none.

Hand-authoring `journeys/<target>.json` is why testgraph is honeyslate-only, and
"registry completeness is manual" is the documented silent-under-selection risk:
honeyslate defines `delete_task` and no hand-authored journey covers it, so a
change to task deletion reports NONE today. This converts authoring cost into
review cost (issue #6).

The split is deliberate: **this module does discovery, an agent does judgment.**
Discovery — which symbols are HTTP entry points, and do they resolve in the index
— is deterministic and better done by `ast` plus the graph. Grouping and naming
are judgment, and a human has to approve them anyway. So the draft this writes is
already valid and runnable on its own, and `skills/testgraph-propose` is the
optional pass that makes it readable. No API key, no runtime dependency: the same
reason `registry.live_drift` parses with `ast` instead of calling RunEcho.

Two safety rules the draft obeys:

  * **Split, never merge.** One journey per handler. Splitting is the safe
    direction — more journeys, each narrower, recall unaffected. Merging two
    distinct flows behind one id hides which of them broke, so the mechanical
    pass never guesses a boundary. The agent merges; this does not.
  * **Never ship an entry the index cannot resolve.** `registry.unresolved`
    treats a journey with no resolvable entry as blocking, so a drafted-but-
    unresolvable entry would take the whole registry down. They are excluded and
    reported instead.

Usage:
    python -m testgraph.propose --repo <path> [--db PATH] [--target NAME]
                                [--out PATH] [--json]
"""
import argparse
import ast
import json
import os
import subprocess
import sys

from . import db as dbmod
from . import registry as reg
from . import select as sel  # for the one definition of "this path is test code"

# Decorator attributes that mark an HTTP entry point across the Python web
# frameworks that share this idiom: FastAPI/APIRouter (`@router.post`), Flask and
# Blueprint (`@app.route`), Starlette, Sanic, AIOHTTP's `@routes.get`. Matching on
# the ATTRIBUTE rather than the receiver name is what makes it framework-agnostic
# — `app`, `router`, `bp`, `routes` and every other local name work unchanged.
HTTP_DECORATORS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options",
    "route", "websocket",
})

# Entry-point classes this scan structurally cannot find. Named in the output
# rather than guessed at: honeyslate's J8 anchors on `scheduler.sweep` and
# `scheduler.start`, which carry no decorator and which a human knew to add. A
# heuristic that tried to infer them would invent journeys; saying "this is a
# blind spot" is the honest answer and is what the skill sends the agent to check.
STRUCTURAL_BLIND_SPOTS = (
    "background jobs and schedulers (no decorator marks them — honeyslate's J8 "
    "is this class)",
    "CLI entry points and management commands",
    "event, queue, and webhook consumers registered by callback rather than "
    "decorator",
    "ASGI/WSGI middleware and lifespan hooks",
)

# Product extensions this scan cannot parse. Kept in sync in spirit with
# select.PRODUCT_EXT — the point is only to COUNT them so the blind spot is
# reported with evidence rather than as boilerplate.
_NON_PYTHON_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
                   ".svelte", ".vue")


def _decorator_routes(node):
    """[(method, path)] for every HTTP-route decorator on `node`.

    `path` is None when the first argument is not a literal string (a computed
    prefix, an f-string): the handler is still a real entry point, so it is kept
    and only its display name degrades.
    """
    routes = []
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        if not isinstance(target, ast.Attribute):
            continue
        if target.attr not in HTTP_DECORATORS:
            continue
        path = None
        if call and call.args and isinstance(call.args[0], ast.Constant):
            if isinstance(call.args[0].value, str):
                path = call.args[0].value
        routes.append((target.attr.upper(), path))
    return routes


def _handlers(tree):
    """[(name, [(method, path)])] for module-level decorated route handlers.

    Module level only, and deliberately: a `def` nested inside another function
    is not importable, so it can never be an entry symbol the registry could
    resolve — the same rule `registry._scan` enforces from the other direction.
    Methods on a class ARE skipped here too; a class-based view is a real pattern
    but its dispatch is framework-specific, so it belongs in the blind-spot list
    rather than in a guess.
    """
    found = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routes = _decorator_routes(stmt)
            if routes:
                found.append((stmt.name, routes))
    return found


def scan(repo):
    """[(relpath, handler_name, [(method, path)])] over every Python source.

    Unparseable files are skipped rather than raised on — a repo with one broken
    module should still get a draft for the rest — and counted in `blind_spots`
    by the caller.
    """
    hits, unparsed = [], []
    for path in sorted(reg.python_sources(repo)):
        rel = os.path.relpath(path, repo)
        # A test module that mounts its own fixture app defines real decorated
        # handlers, and registering them would create journeys for flows no user
        # can reach. Same product/test split `select` applies to diff seeds.
        if sel._is_test(rel.replace(os.sep, "/")):
            continue
        try:
            with open(path, "rb") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError, ValueError):
            unparsed.append(rel)
            continue
        for name, routes in _handlers(tree):
            hits.append((rel, name, routes))
    return hits, unparsed


def _short_id(rel, handler):
    stem = os.path.basename(rel)[:-3] or "mod"
    return "J_%s_%s" % (_slug(stem), _slug(handler))


def _path_id(rel, handler):
    stem = rel[:-3] if rel.endswith(".py") else rel
    return "J_%s_%s" % (_slug(stem), _slug(handler))


def assign_ids(hits):
    """{(relpath, handler): journey_id} — short ids, widened only on collision.

    Route paths were the obvious id source and are NOT unique: `GET /tasks` and
    `GET /tasks/{task_id}` slugify identically once the path parameter is
    stripped. File-stem + handler was the first fix and is not unique either —
    `api/v1/users.py` and `api/v2/users.py` both defining `list_users` collide,
    and because journeys are keyed by id in a dict, the second silently
    OVERWROTE the first. A handler the proposer found would then be missing from
    the registry entirely, which is the silent under-selection this module exists
    to remove. Colliding groups widen to the full path; a trailing counter is the
    backstop so the result is unique whatever the paths look like.
    """
    groups = {}
    for rel, handler, _ in hits:
        groups.setdefault(_short_id(rel, handler), []).append((rel, handler))

    ids, used = {}, set()
    for short, keys in groups.items():
        for rel, handler in keys:
            candidate = short if len(keys) == 1 else _path_id(rel, handler)
            if candidate in used:
                n = 2
                while f"{candidate}_{n}" in used:
                    n += 1
                candidate = f"{candidate}_{n}"
            used.add(candidate)
            ids[(rel, handler)] = candidate
    return ids


def _slug(text):
    out = "".join(c if c.isalnum() else "_" for c in text)
    return out.strip("_") or "x"


def _journey_name(handler, routes):
    labelled = [f"{m} {p}" for m, p in routes if p]
    if not labelled:
        return handler
    # More than one route on one handler is real (an alias path). Both are shown:
    # the name is what a human reads when deciding whether the grouping is right.
    return " + ".join(labelled)


def _blind_spots(repo, unparsed, non_python):
    spots = list(STRUCTURAL_BLIND_SPOTS)
    if non_python:
        spots.append(
            "%d non-Python product file(s) (e.g. %s) — no parser here, so any "
            "handler defined in them is invisible to this scan"
            % (len(non_python), ", ".join(sorted(non_python)[:3]))
        )
    if unparsed:
        spots.append(
            "%d Python file(s) that do not parse (%s) — skipped entirely"
            % (len(unparsed), ", ".join(unparsed[:3]))
        )
    return spots


def _non_python_product(repo):
    found = []
    for root, dirs, files in os.walk(repo):
        reg.prune_dirs(root, dirs)
        for f in files:
            if not f.endswith(_NON_PYTHON_EXT):
                continue
            rel = os.path.relpath(os.path.join(root, f), repo)
            if not sel._is_test(rel.replace(os.sep, "/")):
                found.append(rel)
    return found


# How much history to read when measuring churn. Deep enough to distinguish a
# hot UI component from a stable backend helper, shallow enough that the git call
# stays well under a second on a large repo.
CHURN_DEPTH = 500


def file_churn(repo, depth=CHURN_DEPTH):
    """{repo-relative path: commits touching it} over the last `depth` commits.

    `{}` on any git failure — a shallow clone, a fresh repo with no commits, or a
    plain directory. Callers must treat an empty result as "churn is unknowable"
    and fall back rather than scoring everything as maximally stable.
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", "--format=", "--name-only",
             "-n", str(depth)],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    counts = {}
    for line in out.splitlines():
        path = line.strip()
        if path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def _caller_churn(churn, caller_files):
    """Mean commits-per-calling-file — the volatility of this symbol's fan-in.

    Deliberately the CALLER files, not the symbol's own file. The floor breaks
    when fan-in drops, and fan-in drops when call sites are deleted, so the
    definition's own history says nothing useful: `button.tsx` is a stable file
    whose 185 callers churn constantly, and scoring on the definition would rank
    it as the safest pin in the repo (issue #43).
    """
    if not caller_files:
        return 0.0
    return sum(churn.get(f, 0) for f in caller_files) / len(caller_files)


# Fraction of the OBSERVED inbound-edge count used as each spot-check floor. The
# check exists to catch an index that silently lost edges (`integrity.py`: an
# interrupted run left blast radius 85% wrong), so the floor has to sit below
# normal churn and far above corruption. 0.8 is a starting point a human revises
# at approval time, not a measured constant — which is why it is written into the
# draft as data rather than hidden in the guard.
SPOT_CHECK_FLOOR = 0.8

# The floor must leave room for at least this many call sites to disappear before
# the guard fires. Derived, not guessed: at `SPOT_CHECK_FLOOR = 0.8` a symbol with
# 3 inbound edges has a floor of 2, so deleting ONE caller blocks every run. Small
# fan-in is therefore the most fragile pin of all, whatever its churn — the 20%
# band has to be wider than the noise it is meant to ignore. `count - floor >= 2`
# works out to a minimum fan-in of 10.
MIN_TOLERANCE_EDGES = 2


# How many candidates to rank before taking the picks. Wide enough that a churn
# ranking can reach past a block of volatile frontend symbols to a stable one.
SPOT_CHECK_POOL = 40


def _spot_checks(conn, exclude, churn, limit=2):
    """Pinned STABLE high-fan-in symbols for `integrity.check`, and the ranked
    candidates behind them, as `(checks, candidates)`.

    **Ranked by caller-file churn first, fan-in second** (issue #43). Ranking on
    fan-in alone picked `Button` (185 edges, `frontend/src/components/ui/button.tsx`)
    on coriolis-local and `get` (`frontend/src/api/client.ts`) on
    llm_history_audit. The spot-check is the *blocking* half of the guard and its
    printed remedy is `codegraph index`, so a pin on a shared UI component turns
    "delete a few <Button> usages" into a permanent block with a remedy that
    cannot clear it. Fan-in says how depended-upon a symbol is; it does not say
    how stable that number is, and the guard needs the second property.

    When `churn` is empty — a shallow clone, a repo with no commits, a plain
    directory — every candidate scores 0 and the order collapses to fan-in, i.e.
    exactly the pre-#43 behaviour. Degraded, never broken, and the draft records
    which mode produced the pins.

    Two exclusions, both load-bearing:

      * **Entry symbols.** A handler's fan-in is structurally ~0 — the framework
        calls it, the codebase does not — so pinning one sets a floor of 0 and
        the guard passes on a wrecked index.
      * **Test files, on BOTH ends of the edge.** The first run against
        honeyslate pinned `login` from `backend/tests/conftest.py` with a floor
        of 36 — the guard exists to detect a damaged PRODUCT graph, and anchoring
        it to a test fixture makes it fire on ordinary test refactors. Excluding
        test *targets* alone was still half a fix: the floor is compared against
        `caller_edge_count`, which counts test call sites too, so a floor derived
        from them turns "delete three tests" into a BLOCK whose printed remedy
        (`codegraph index`) can never clear it. `exclude_source` drops them.
    """
    excluded_names = {n for n, _ in exclude}
    is_test = lambda p: sel._is_test(p.replace(os.sep, "/"))

    candidates = []
    for name, file_path, count, callers in dbmod.top_fanin_nodes(
        conn, SPOT_CHECK_POOL, exclude_source=is_test
    ):
        if name in excluded_names or is_test(file_path):
            continue
        floor = int(count * SPOT_CHECK_FLOOR)
        if floor < 2:
            continue  # a floor of 0 or 1 asserts nothing
        if count - floor < MIN_TOLERANCE_EDGES:
            continue  # one ordinary deletion would trip it
        volatility = round(_caller_churn(churn, callers), 2)
        candidates.append({
            "name": name,
            "file": file_path,
            "fan_in": count,
            "caller_churn": volatility,
            # Sensitivity per unit of noise. Ranking on churn ASCENDING first was
            # tried and is WRONG: fan-in becomes a pure tie-break, and since some
            # symbol always has near-zero churn the pick collapses to the quietest
            # — honeyslate went from `get_settings` (15 of 19 edges) to
            # `hash_token` (2 of 3), trading a real canary for an obscure one.
            # Quiet is not the goal; quiet AND load-bearing is.
            "score": round(count / (1.0 + volatility), 2),
            "min_caller_edges": floor,
        })

    candidates.sort(key=lambda c: (-c["score"], -c["fan_in"], c["name"]))
    checks = {
        c["name"]: {"min_caller_edges": c["min_caller_edges"], "file": c["file"]}
        for c in candidates[:limit]
    }
    return checks, candidates


def propose(repo, db_path, target):
    """Draft registry + the evidence behind it, as one dict.

    The draft is a complete, valid registry: it can be handed straight to
    `select` (with a loud unapproved warning) before any agent touches it.
    """
    conn = dbmod.connect(db_path)
    hits, unparsed = scan(repo)
    ids = assign_ids(hits)

    journeys, candidates, unresolved = {}, [], []
    resolved_entries = set()
    for rel, handler, routes in hits:
        node_ids = dbmod.resolve_symbol(conn, handler, rel)
        record = {
            "handler": handler,
            "file": rel,
            "routes": [{"method": m, "path": p} for m, p in routes],
        }
        if not node_ids:
            # Excluded, not shipped broken: `registry.unresolved` blocks on a
            # journey whose entries do not resolve, so one unresolvable draft
            # entry would take the entire registry down rather than degrade.
            record["reason"] = (
                "no node in the index for this symbol — the index predates the "
                "file, or `codegraph index` has not been run"
            )
            unresolved.append(record)
            continue
        record["fan_in"] = sum(dbmod.caller_edge_count(conn, n) for n in node_ids)
        record["journey"] = ids[(rel, handler)]
        candidates.append(record)
        resolved_entries.add((handler, rel))
        journeys[record["journey"]] = {
            "name": _journey_name(handler, routes),
            "entries": [{"name": handler, "file": rel}],
            "route": [f"{m} {p or '?'}" for m, p in routes],
        }

    # One journey per resolved candidate, always. A count mismatch means two
    # candidates shared an id and one was overwritten out of the dict — a handler
    # found and then silently dropped. Loud here rather than absent from a draft
    # someone approves.
    if len(journeys) != len(candidates):
        raise AssertionError(
            f"{len(candidates)} candidates collapsed into {len(journeys)} "
            f"journeys — journey ids are not unique"
        )
    candidates.sort(key=lambda c: (c["file"], c["journey"]))
    churn = file_churn(repo)
    checks, check_candidates = _spot_checks(conn, resolved_entries, churn)
    draft = {
        "target": target,
        "note": (
            "DRAFT — proposed by testgraph.propose, not reviewed. One journey per "
            "route handler; group and rename them, check the blind spots, then set "
            "\"approved\": true."
        ),
        "approved": False,
        "proposed_by": "testgraph.propose",
        "journeys": journeys,
        "codegraph_schema_version": dbmod.schema_version(conn),
        "spot_checks": checks,
        # Shipped beside the picks so the reviewer can swap one without
        # re-deriving anything -- the same "tool discovers, agent judges" split
        # the journeys themselves follow. Pinning nothing at all was rejected: it
        # leaves a draft whose guard does not work until someone acts, and a
        # draft is meant to be runnable.
        "spot_check_candidates": check_candidates[:8],
        "spot_check_basis": (
            "caller-file churn over the last %d commits, then fan-in"
            % CHURN_DEPTH
            if churn else
            "fan-in only — no git history available, so stability is unmeasured"
        ),
        "blind_spots": _blind_spots(repo, unparsed, _non_python_product(repo)),
        "unresolved_candidates": unresolved,
    }
    return {
        "target": target,
        "repo": repo,
        "draft": draft,
        "candidates": candidates,
        "unresolved_candidates": unresolved,
        "blind_spots": draft["blind_spots"],
    }


def _render(result, out_path):
    draft = result["draft"]
    lines = [
        f"repo: {result['repo']}",
        f"found {len(result['candidates'])} route handler(s) "
        f"-> {len(draft['journeys'])} draft journey(s)",
    ]
    # No fan-in column. It is recorded in the JSON bundle, but printing it put a
    # column of zeros beside all 17 honeyslate handlers: an entry point is called
    # by the framework, not by the codebase, so its inbound-edge count is
    # structurally ~0 and ranking by it orders nothing.
    for c in result["candidates"]:
        route = ", ".join(f"{r['method']} {r['path'] or '?'}" for r in c["routes"])
        fan = f"  fan-in {c['fan_in']}" if c["fan_in"] else ""
        lines.append(f"  {c['journey']}  {route}  ({c['file']}){fan}")
    for u in result["unresolved_candidates"]:
        lines.append(
            f"  EXCLUDED {u['handler']} ({u['file']}): {u['reason']}"
        )
    lines.append("blind spots — this scan cannot see:")
    for b in draft["blind_spots"]:
        lines.append(f"  - {b}")
    if not draft["spot_checks"]:
        lines.append(
            "  - no symbol had enough inbound edges to pin an integrity "
            "spot-check; add one by hand before approving"
        )
    else:
        lines.append(f"integrity spot-checks ({draft['spot_check_basis']}):")
        for c in draft["spot_check_candidates"][:len(draft["spot_checks"])]:
            lines.append(
                f"  {c['name']}  >= {c['min_caller_edges']} of {c['fan_in']} "
                f"edges  churn {c['caller_churn']}  ({c['file']})"
            )
    if out_path is None:
        lines.append(
            "NO JOURNEYS, nothing written — this repo has no decorator-style HTTP "
            "entry points, so its journeys start somewhere this scan cannot see "
            "(the blind spots above). Write a registry by hand from that list, or "
            "conclude testgraph does not fit this repo."
        )
    else:
        lines.append(f"wrote {out_path}  (approved: false)")
        lines.append(
            "next: group and rename the journeys, resolve the blind spots, then set "
            "\"approved\": true"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testgraph.propose")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--db", default=None,
                    help="defaults to <repo>/.codegraph/codegraph.db")
    ap.add_argument("--target", default=None,
                    help="registry target name (defaults to the repo dir name)")
    ap.add_argument("--out", default=None,
                    help="draft registry path (default journeys/<target>.draft.json)")
    ap.add_argument("--json", dest="json_out", action="store_true",
                    help="print the full candidate bundle instead of a summary")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    db_path = args.db or os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.exists(db_path):
        print(
            f"no CodeGraph index at {db_path} — run `codegraph index {repo}` first",
            file=sys.stderr,
        )
        return 2
    # `<repo>/main` is the worktree layout used across these projects, so the leaf
    # directory is "main" for almost every repo. Walking up one level gives the
    # name a human would recognise.
    target = args.target
    if not target:
        base = os.path.basename(repo)
        target = os.path.basename(os.path.dirname(repo)) if base == "main" else base

    result = propose(repo, db_path, target)
    out_path = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "journeys", f"{target}.draft.json",
    )
    # A draft with no journeys is NOT written. It would be a valid, approvable
    # registry that answers a confident NONE for every change — the worst artifact
    # this tool could leave behind. The scan still reports what it looked for, so
    # the run is diagnostic rather than empty.
    written = bool(result["draft"]["journeys"])
    if written:
        # `os.path.dirname` is "" for a bare `--out draft.json`, and makedirs("")
        # raises FileNotFoundError after the whole scan has already run. Same
        # guard `export.main` uses.
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(result["draft"], fh, indent=2)
            fh.write("\n")

    if args.json_out:
        print(json.dumps(result, indent=2))
    else:
        print(_render(result, out_path if written else None))
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
