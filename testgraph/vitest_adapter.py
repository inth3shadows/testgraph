"""Run a Vitest suite once and record which journeys its tests ACTUALLY
exercised, from a per-test runtime trace (#122).

The Vitest counterpart of `pytest_adapter`: same attribution (a test covers a
journey when it RAN one of the journey's entry symbols), same ledger rows,
same `edge_provenance="trace"`. Only the two halves of the run differ:

  - **What ran**: `harness/plugin/tgtrace-vitest.ts`, a setup file using V8
    precise coverage, writes one part file per test file:
    `{"tests": {"<relfile>::<describe> > <title>": [[relpath, functionName], ...]}}`.
  - **Verdicts**: Vitest's JSON reporter, keyed by the same id.

Nothing is written into the target repo. The setup file is injected through a
generated config in a temp dir that imports the repo's own config and
`mergeConfig`s the setup file in; Vitest has no `--setupFiles` CLI flag. The
temp dir gets a `node_modules` of symlinks to the repo's entries, so the
generated config and the setup file resolve `vitest` exactly as the repo's
tests do, while the config bundle Vite writes (`.vite-temp`) lands in the temp
dir. Vitest's own cache (`node_modules/.vite`) is written where any
`vitest run` in the repo writes it. (`--configLoader runner` would avoid the
bundle but does not define `__dirname`, which real configs use.)

A broken or partial run is never a verdict: a missing trace for a test that
passed or failed, a duplicate test id, a file that failed outside any test, a
report of the wrong shape, and a `projects`/workspace config (which does not
inherit `setupFiles`) are all refusals.

Selecting only some journeys' tests is `verify --runner vitest`'s job (the
`@tg:` tag helpers below). Deliberately not here yet: anonymous functions (they need source maps, which only
`reconcile` would use). See `~/.claude/plans/testgraph-154-then-vitest-adapter.md`.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import db as dbmod
from . import ledger
from . import pytest_adapter as pa
from . import registry as reg
from . import results as res

PLUGIN_NAME = "tgtrace-vitest.ts"
BACKEND = "v8-precise-coverage"
CONFIG_NAMES = tuple(
    f"{stem}.config.{ext}"
    for stem in ("vitest", "vite")
    for ext in ("ts", "mts", "cts", "js", "mjs", "cjs")
)
# Vitest JSON reporter status -> the run-result status `results` defines.
STATUS = {"passed": "pass", "failed": "fail", "skipped": "skip",
          "todo": "skip"}


def find_node_modules(repo):
    """The nearest `node_modules` at or above `repo` that holds vitest, or None.

    Walked up because a workspace hoists dependencies to its root."""
    d = os.path.abspath(repo)
    while True:
        nm = os.path.join(d, "node_modules")
        if os.path.isfile(os.path.join(nm, "vitest", "package.json")):
            return nm
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def link_node_modules(source, dest):
    """A real `dest` directory holding a symlink to each entry of `source`.

    Not one symlink to the directory: Vite bundles a config into
    `node_modules/.vite-temp` beside the config file, which through a
    directory symlink would be the target's own node_modules. Here it lands in
    `dest`, which dies with the temp dir. `.vite` and `.vite-temp` are not
    linked, so no stale cache is read either."""
    os.makedirs(dest, exist_ok=True)
    for entry in os.listdir(source):
        if entry not in (".vite", ".vite-temp") and not os.path.lexists(os.path.join(dest, entry)):
            os.symlink(os.path.join(source, entry), os.path.join(dest, entry))


def find_config(repo):
    """The repo's own Vitest (else Vite) config file, or None."""
    for name in CONFIG_NAMES:
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            return path
    return None


WORKSPACE_STEMS = tuple(
    f"vitest.workspace.{ext}" for ext in ("ts", "mts", "cts", "js", "mjs", "cjs", "json"))
PROJECTS_MARKER = "tgtrace-vitest: test.projects is not supported yet"
PROJECTS_REFUSAL = ("the repo's Vitest config uses `test.projects` (or a workspace "
                    "file), which this adapter does not support yet: projects do "
                    "not inherit `setupFiles`, so no test would be traced")


def find_workspace(repo):
    """A `vitest.workspace.*` file in the repo, or None."""
    for name in WORKSPACE_STEMS:
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            return path
    return None


def config_source(repo, base_config, setup_file):
    """The generated config: the repo's config plus our setup file.

    `mergeConfig` concatenates `setupFiles`, so the repo's own setup files
    still run, before ours. A config exported as a function is called the way
    Vitest calls it for `vitest run`."""
    # `await` takes a function's result and a Promise default export alike.
    # No `root`: the run's cwd is the repo, which is already Vitest's default,
    # and forcing one would override a `root` the repo sets itself.
    base = (
        f"import base from {json.dumps(base_config)};\n"
        "const resolved = await (typeof base === \"function\"\n"
        "  ? base({ command: \"serve\", mode: \"test\" })\n"
        "  : base);\n"
        "if (resolved?.test?.projects || resolved?.test?.workspace) {\n"
        f"  throw new Error({json.dumps(PROJECTS_MARKER)});\n"
        "}\n"
    ) if base_config else "const resolved = {};\n"
    return (
        "import { defineConfig, mergeConfig } from \"vitest/config\";\n"
        + base
        + "export default mergeConfig(resolved ?? {}, defineConfig({\n"
        f"  test: {{ setupFiles: [{json.dumps(setup_file)}] }},\n"
        "}));\n"
    )


def test_id(root, file_path, ancestors, title):
    """`<relfile>::<describe> > ... > <title>`, as the setup file builds it."""
    rel = os.path.relpath(file_path, root).replace(os.sep, "/")
    return f"{rel}::{' > '.join([*ancestors, title])}"


def _bad_report(why):
    return None, f"vitest's JSON report has an unexpected shape ({why}); no verdict was recorded"


def parse_report(report, root):
    """Vitest JSON reporter output -> ({join_key: {"status", "test_id"}}, error).

    Keyed through `pytest_adapter._join_key`, the key `attribute` joins on, so
    the generic attribution code needs no Vitest branch. The error is a
    refusal: the report is malformed, a test id appears twice (the join would
    keep one and mask the other, so a failure could vanish), or a file failed
    outside any test (import or collection error, a throwing beforeAll or
    afterAll), which the per-test rows cannot show."""
    results = report.get("testResults")
    if not isinstance(results, list):
        return _bad_report("`testResults` is missing or not a list")
    out = {}
    for file_result in results:
        if not isinstance(file_result, dict) or not isinstance(file_result.get("name"), str):
            return _bad_report("a `testResults` entry is not an object with a string `name`")
        cases = file_result.get("assertionResults")
        if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
            return _bad_report(f"`assertionResults` of {file_result['name']} is not a list of objects")
        rel = os.path.relpath(file_result["name"], root).replace(os.sep, "/")
        if file_result.get("status") == "failed" and not any(
                c.get("status") == "failed" for c in cases):
            # Vitest reports tests under a throwing beforeAll as "skipped", so
            # only the file's own status says it broke; reading those tests as
            # skips would hide the failure.
            message = str(file_result.get("message") or "").strip().splitlines()
            return None, (f"{rel} failed outside any test (an import or collection "
                          f"error, or a beforeAll/afterAll that threw); no verdict "
                          f"was recorded" + (f"\n  {message[0][:300]}" if message else ""))
        for case in cases:
            ancestors, title = case.get("ancestorTitles", []), case.get("title")
            if (not isinstance(ancestors, list)
                    or not all(isinstance(a, str) for a in ancestors)
                    or not isinstance(title, str)):
                return _bad_report(f"a test in {rel} lacks a string `title` or a "
                                   f"list-of-strings `ancestorTitles`")
            tid = test_id(root, file_result["name"], ancestors, title)
            if case.get("status") == "pending":
                # The reporter's "pending" is a test that never finished (a
                # killed worker, a bail), not a skip: a skip is "skipped".
                return None, (f"test {tid!r} never finished (status pending: a worker "
                              f"died or the run was cut short); no verdict was recorded")
            key = pa._join_key(tid)
            if key in out:
                return None, (f"two tests share the id {tid!r} (the same title in the "
                              f"same describe, e.g. test.each with a repeated row); the "
                              f"verdicts cannot be told apart, so no verdict was recorded")
            out[key] = {"status": STATUS.get(case.get("status"), "skip"), "test_id": tid}
    return out, None


def merge_parts(out_dir):
    """Merge the per-test-file part files into one tgtrace-shaped payload.

    Returns (trace, error). A part that is not whole is a broken run, the same
    refusal as a truncated tgtrace trace."""
    tests = {}
    paths = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    for path in paths:
        try:
            with open(path) as fh:
                part = json.load(fh)
        except (OSError, ValueError) as exc:
            return None, (f"a tgtrace-vitest part is truncated or not JSON "
                          f"({exc}); no verdict was recorded")
        if not isinstance(part, dict) or pa.payload_shape_error(part):
            return None, (f"{os.path.basename(path)} is not a tgtrace-vitest part; "
                          f"no verdict was recorded")
        dups = part.get("duplicates") or []
        if not isinstance(dups, list):
            return None, (f"{os.path.basename(path)} is not a tgtrace-vitest part; "
                          f"no verdict was recorded")
        if dups:
            return None, (f"two tests share the id {dups[0]!r} (the same title in the "
                          f"same describe, e.g. test.each with a repeated row); their "
                          f"traces cannot be told apart, so no verdict was recorded")
        tests.update(part["tests"])
    trace = {"backend": BACKEND, "tests": tests, "declared": {}}
    return trace, None


def execute(repo, plugin, node_modules, tmp, vitest_args=(), runner=None):
    """One traced Vitest run in `tmp`: (proc, trace, verdicts, error).

    Any error is a refusal: nothing here may turn a broken or partial run into
    a verdict."""
    # The real path, for everything compared: V8 script URLs and Vitest's test
    # file paths are symlink-resolved, so a root spelled through a symlink
    # would never prefix-match them and every trace would be empty.
    repo = os.path.realpath(repo)
    workspace = find_workspace(repo)
    if workspace:
        return None, None, None, f"{PROJECTS_REFUSAL} ({os.path.basename(workspace)})"
    parts = os.path.join(tmp, "parts")
    os.makedirs(parts, exist_ok=True)
    report_path = os.path.join(tmp, "results.json")
    setup = os.path.join(tmp, PLUGIN_NAME)
    shutil.copyfile(plugin, setup)
    config = os.path.join(tmp, "tg.vitest.config.mts")
    with open(config, "w") as fh:
        fh.write(config_source(repo, find_config(repo), setup))
    if node_modules:
        link_node_modules(node_modules, os.path.join(tmp, "node_modules"))
    env = dict(os.environ, TGTRACE_OUT_DIR=parts, TGTRACE_ROOT=repo)
    cmd = [os.path.join(node_modules or "", ".bin", "vitest"), "run",
           "--config", config,
           "--coverage.enabled=false",
           "--reporter=json", f"--outputFile={report_path}", *vitest_args]
    runner = runner or (lambda: subprocess.run(
        cmd, cwd=repo, env=env, capture_output=True, text=True))
    proc = runner()

    stderr = getattr(proc, "stderr", "") or ""
    tail = stderr.strip()[-400:]
    if not os.path.isfile(report_path):
        if PROJECTS_MARKER in stderr:
            return proc, None, None, PROJECTS_REFUSAL
        return proc, None, None, (
            f"vitest exited {getattr(proc, 'returncode', '?')} and wrote no "
            f"JSON report; no verdict was recorded"
            + (f"\n  vitest stderr (tail): {tail}" if tail else ""))
    try:
        with open(report_path) as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        return proc, None, None, (f"vitest's JSON report is truncated or not "
                                  f"JSON ({exc}); no verdict was recorded")
    if not isinstance(report, dict):
        return proc, None, None, "vitest's JSON report is not an object"
    verdicts, err = parse_report(report, repo)
    if err:
        return proc, None, None, err
    trace, err = merge_parts(parts)
    if err:
        return proc, None, None, err
    traced = {pa._join_key(t) for t in trace["tests"]}
    # A test that passed or failed ran its beforeEach, so it has a trace. Skipped
    # and todo tests never ran a hook and legitimately have none. A missing
    # trace means a worker died or the setup file never ran, and attributing
    # from the rest would silently shrink coverage.
    ran = [k for k, v in verdicts.items() if v["status"] in ("pass", "fail")]
    missing = [k for k in ran if k not in traced]
    if missing and len(missing) == len(ran):
        return proc, None, None, (
            f"vitest ran {len(ran)} test(s) (e.g. {verdicts[missing[0]]['test_id']!r}) "
            f"but tgtrace-vitest traced none — the setup file did not load or did "
            f"not reach the tests (a projects/workspace config does not inherit "
            f"setupFiles); no verdict was recorded"
            + (f"\n  vitest stderr (tail): {tail}" if tail else ""))
    if missing:
        return proc, None, None, (
            f"{len(missing)} of {len(ran)} passed/failed tests have no trace "
            f"(e.g. {verdicts[missing[0]]['test_id']!r}) — a worker died or a hook "
            f"threw before the trace was written; no verdict was recorded")
    return proc, trace, verdicts, None


def locate(repo, _runner=None):
    """(plugin, node_modules, error): what a traced run needs, or why it can't."""
    plugins = pa.plugin_dir()
    plugin = os.path.join(plugins, PLUGIN_NAME) if plugins else None
    if not plugin or not os.path.isfile(plugin):
        return None, None, (f"cannot find {PLUGIN_NAME}: it ships beside tgtrace.py "
                            f"(testgraph/_plugin/ installed, harness/plugin/ in a checkout)")
    # From the real path: `execute` resolves the repo the same way.
    node_modules = find_node_modules(os.path.realpath(repo))
    if node_modules is None and _runner is None:
        return None, None, (f"no node_modules with vitest at or above {repo} — install "
                            f"the repo's dependencies first")
    return plugin, node_modules, None


def run_traced(repo, plugin, node_modules, vitest_args=(), _runner=None, _workdir=None):
    """`execute` in a temp dir that dies with the run: (proc, trace, verdicts, error).
    The one traced-run path for this adapter and `verify --runner vitest`."""
    if _workdir is not None:
        return execute(repo, plugin, node_modules, _workdir, vitest_args, _runner)
    with tempfile.TemporaryDirectory() as tmp:
        return execute(repo, plugin, node_modules, tmp, vitest_args, _runner)


#: A test declares a journey with this tag in its own title or a describe title,
#: the same string Playwright's tag uses (results.DEFAULT_TAG_CONVENTIONS).
TAG_TEMPLATE = res.DEFAULT_TAG_CONVENTIONS["playwright"]


def name_pattern(journeys):
    """`(^|\\s)(@tg:J1|@tg:J2)(\\s|$)`: a Vitest `-t` for exactly these journeys.

    Vitest matches `-t` against the test's full name, the file and every
    describe title joined by single spaces. Both boundaries are load-bearing:
    without the trailing one `@tg:J1` also selects `@tg:J10`, without the
    leading one `x@tg:J1` would select J1."""
    alts = "|".join(re.escape(res.format_tag(TAG_TEMPLATE, j)) for j in journeys)
    return f"(^|\\s)({alts})(\\s|$)"


def declared(verdicts, journey_ids, selected):
    """{test_id: [journey, ...]} for the tests `name_pattern(selected)` ran.

    A test declares every registry journey whose tag is a whole token of its
    describe chain or title; tags the registry does not define are ignored, as
    an unknown `covers(...)` id would be. A test whose tags name no selected
    journey was filtered out by `-t` and declares nothing in this run."""
    wanted = set(selected)
    out = {}
    for v in verdicts.values():
        tid = v["test_id"]
        tokens = set(tid.split("::", 1)[-1].split())
        js = [j for j in journey_ids if res.format_tag(TAG_TEMPLATE, j) in tokens]
        if wanted.intersection(js):
            out[tid] = js
    return out


def run(repo, registry_path=None, db_path=None, vitest_args=(), commit=None,
        append=None, trace_out=None, _runner=None, _workdir=None):
    """Run the suite, attribute journeys from the trace, write the rows.

    Returns (summary, error), as `pytest_adapter.run` does: a red suite is a
    result, a run that cannot be read is a refusal."""
    repo = os.path.abspath(repo)
    registry_path = registry_path or reg.resolve_for_repo(repo)
    if registry_path is None:
        return None, (f"no journey registry for {reg.repo_name(repo)} — looked in "
                      f"{reg.where_it_looked(repo)}")
    try:
        registry = reg.load(registry_path)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {registry_path}: {exc}"

    db_path = db_path or os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.isfile(db_path):
        return None, f"no codegraph index at {db_path} — run `codegraph init` in {repo}"

    plugin, node_modules, err = locate(repo, _runner)
    if err:
        return None, err

    if trace_out:
        bad = pa.trace_out_error(trace_out)
        if bad:
            return None, bad

    sha = ledger.resolve_commit(repo, commit or "HEAD")
    if sha is None:
        return None, (f"cannot resolve {commit or 'HEAD'!r} in {repo} — an "
                      f"unjoinable key would sit in `unasked` forever")

    proc, trace, verdicts, err = run_traced(repo, plugin, node_modules, vitest_args,
                                            _runner=_runner, _workdir=_workdir)
    if err:
        return None, err
    if trace_out:
        pa.write_json_atomic(trace_out, trace)

    conn = dbmod.connect(db_path)
    try:
        entries = pa.journey_entries(conn, registry)
        attribution = pa.attribute(conn, trace["tests"], entries, verdicts)
        written = pa.write_outcomes(reg.repo_name(repo), sha, attribution, append=append)
        summary = dict(attribution)
        summary.update({
            "repo": reg.repo_name(repo),
            "commit": sha,
            "rows_written": len(written),
            "runner": "vitest",
            "runner_exit": getattr(proc, "returncode", None),
            "backend": BACKEND,
            "journeys_registered": len(entries),
        })
        return summary, None
    finally:
        conn.close()


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="testgraph.vitest_adapter",
        description="run a Vitest suite under tgtrace-vitest and record which "
                    "journeys its tests exercised",
        epilog="Put vitest's own flags after `--`.\n"
               "Exit: 0 recorded, 2 refused (no readable run).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--commit", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="attribute and report, but write NO ledger rows")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trace-out", default=None,
                    help="also write the merged trace here, for testgraph.reconcile")
    ap.add_argument("vitest_args", nargs="*")
    args = ap.parse_args(argv)

    summary, err = run(
        args.repo, registry_path=args.registry, db_path=args.db, commit=args.commit,
        vitest_args=tuple(args.vitest_args), trace_out=args.trace_out,
        append=(lambda row: False) if args.dry_run else None,
    )
    if err:
        if args.json:
            print(json.dumps({"refused": True, "error": err}, indent=2, sort_keys=True))
        else:
            print(f"testgraph vitest-adapter: {err}", file=sys.stderr)
        return 2
    summary["dry_run"] = bool(args.dry_run)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    registry = None
    path = args.registry or reg.resolve_for_repo(args.repo)
    if path:
        try:
            registry = reg.load(path)
        except (OSError, ValueError):
            registry = None
    print(pa.render(summary, registry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
