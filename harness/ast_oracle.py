"""An INDEPENDENT reachability oracle, built from Python's own `ast` module.

Why this exists: the seeded-regression eval must not score testgraph against
itself. If the oracle were computed from the same CodeGraph edges the selector
walks, every missing edge would be missing from both sides and the experiment
would confirm whatever the graph already believed.

So this builds its own call graph from source text, and in the OPPOSITE
direction: forward from each journey entry to everything it can reach, whereas
the selector walks backward from changed symbols to entries. Same relation,
independently derived.

Deliberately coarse: calls are matched by BARE NAME (`ast.Name.id` or
`ast.Attribute.attr`), so same-named functions in different modules collide and
the closure over-approximates. That is the safe direction for a recall oracle —
an over-approximating oracle makes the test HARDER to pass, never easier.

Known blind spots (shared with any static approach): dynamic dispatch,
getattr-driven calls, and framework-driven entry points reached only through a
decorator registry. Disagreements are reported for adjudication, never silently
reconciled.
"""
import ast
import os


def _is_test(rel):
    base = os.path.basename(rel)
    return (
        "/tests/" in rel
        or "/e2e/" in rel
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


def _called_names(node):
    """Names referenced inside `node`: direct call targets, plus bare names.

    Bare names are kept because passing a function as a callback is a real
    dependency — `background_tasks.add_task(reconcile, id)` is how honeyslate
    reaches `reconcile`, and an oracle blind to it would under-approximate.

    They are filtered against the set of actually-defined functions by
    `build()`; unfiltered, every local variable became a phantom edge, and
    because closures are name-keyed those phantoms merged unrelated modules
    together. That defect produced two false "selector missed a journey"
    findings on 2026-07-29, both disproved by reading the call paths by hand.
    """
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        elif isinstance(sub, ast.Name):
            out.add(sub.id)
    return out


def build(repo_root):
    """Return (sites, calls, module_deps).

    sites       : {(rel_path, func_name): (start_line, end_line)}
    calls       : {func_name: set(callee bare names)}   -- name-keyed, coarse
    module_deps : {rel_path: set(names called at module scope)}
    """
    sites, calls, module_deps = {}, {}, {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames if d not in (".git", ".codegraph", "__pycache__",
                                             "node_modules", ".venv", "venv")
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo_root)
            if _is_test(rel):
                continue
            try:
                tree = ast.parse(open(full, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue

            mod_names = set()
            for top in tree.body:
                if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)):
                    continue
                mod_names |= _called_names(top)
            module_deps[rel] = mod_names

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end = getattr(node, "end_lineno", node.lineno)
                    sites[(rel, node.name)] = (node.lineno, end)
                    calls.setdefault(node.name, set()).update(_called_names(node))

    # Keep only references that name a function actually defined in this repo.
    # Everything else is a local variable, a stdlib name, or a third-party
    # symbol, and as a phantom edge it would silently merge unrelated closures.
    defined = {name for (_rel, name) in sites}
    calls = {k: (v & defined) for k, v in calls.items() if k in defined}
    module_deps = {k: (v & defined) for k, v in module_deps.items()}
    return sites, calls, module_deps


def forward_closure(calls, module_deps, seed_names, seed_files):
    """Every name reachable downstream of `seed_names`.

    `seed_files` seeds the module-scope dependencies of the entries' own files:
    honeyslate binds module-level singletons (`_settings = get_settings()`), so a
    journey genuinely depends on names its module touches at import time, not
    only on what its handler body calls.
    """
    frontier = set(seed_names)
    for f in seed_files:
        frontier |= module_deps.get(f, set())
    seen = set()
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        frontier |= calls.get(name, set()) - seen
    return seen


def journey_oracle(repo_root, registry):
    """{journey_id: set(function names that journey can reach)} — the oracle."""
    sites, calls, module_deps = build(repo_root)
    oracle = {}
    for jid, spec in registry["journeys"].items():
        names = {e["name"] for e in spec["entries"]}
        files = {e["file"] for e in spec["entries"] if "file" in e}
        oracle[jid] = forward_closure(calls, module_deps, names, files)
    return oracle, sites
