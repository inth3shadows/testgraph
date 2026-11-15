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
temp dir gets a `node_modules` symlink to the repo's, so the generated config
and the setup file resolve `vitest` exactly as the repo's tests do.

Deliberately not here yet: selecting only some journeys' tests (verify's job;
next step), and anonymous functions (they need source maps, which only
`reconcile` would use). See `~/.claude/plans/testgraph-154-then-vitest-adapter.md`.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import db as dbmod
from . import ledger
from . import pytest_adapter as pa
from . import registry as reg

PLUGIN_NAME = "tgtrace-vitest.ts"
BACKEND = "v8-precise-coverage"
CONFIG_NAMES = tuple(
    f"{stem}.config.{ext}"
    for stem in ("vitest", "vite")
    for ext in ("ts", "mts", "cts", "js", "mjs", "cjs")
)
# Vitest JSON reporter status -> the run-result status `results` defines.
STATUS = {"passed": "pass", "failed": "fail", "skipped": "skip",
          "pending": "skip", "todo": "skip"}


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


def find_config(repo):
    """The repo's own Vitest (else Vite) config file, or None."""
    for name in CONFIG_NAMES:
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            return path
    return None


def config_source(repo, base_config, setup_file):
    """The generated config: the repo's config plus our setup file.

    `mergeConfig` concatenates `setupFiles`, so the repo's own setup files
    still run, before ours. A config exported as a function is called the way
    Vitest calls it for `vitest run`."""
    base = (
        f"import base from {json.dumps(base_config)};\n"
        "const resolved = typeof base === \"function\"\n"
        "  ? await base({ command: \"serve\", mode: \"test\" })\n"
        "  : base;\n"
    ) if base_config else "const resolved = {};\n"
    return (
        "import { defineConfig, mergeConfig } from \"vitest/config\";\n"
        + base
        + "export default mergeConfig(resolved ?? {}, defineConfig({\n"
        f"  root: {json.dumps(os.path.abspath(repo))},\n"
        f"  test: {{ setupFiles: [{json.dumps(setup_file)}] }},\n"
        "}));\n"
    )


def test_id(root, file_path, ancestors, title):
    """`<relfile>::<describe> > ... > <title>`, as the setup file builds it."""
    rel = os.path.relpath(file_path, root).replace(os.sep, "/")
    return f"{rel}::{' > '.join([*ancestors, title])}"


def parse_report(report, root):
    """Vitest JSON reporter output -> {join_key: {"status": ...}}.

    Keyed through `pytest_adapter._join_key`, the key `attribute` joins on, so
    the generic attribution code needs no Vitest branch."""
    out = {}
    for file_result in report.get("testResults", []):
        for case in file_result.get("assertionResults", []):
            tid = test_id(root, file_result["name"], case.get("ancestorTitles", []),
                          case.get("title", ""))
            out[pa._join_key(tid)] = {
                "status": STATUS.get(case.get("status"), "skip"),
                "test_id": tid,
            }
    return out


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
        tests.update(part["tests"])
    trace = {"backend": BACKEND, "tests": tests, "declared": {}}
    return trace, None


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

    plugins = pa.plugin_dir()
    plugin = os.path.join(plugins, PLUGIN_NAME) if plugins else None
    if not plugin or not os.path.isfile(plugin):
        return None, (f"cannot find {PLUGIN_NAME}: it ships beside tgtrace.py "
                      f"(testgraph/_plugin/ installed, harness/plugin/ in a checkout)")

    db_path = db_path or os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.isfile(db_path):
        return None, f"no codegraph index at {db_path} — run `codegraph init` in {repo}"

    node_modules = find_node_modules(repo)
    if node_modules is None and _runner is None:
        return None, (f"no node_modules with vitest at or above {repo} — install "
                      f"the repo's dependencies first")

    if trace_out:
        bad = pa.trace_out_error(trace_out)
        if bad:
            return None, bad

    sha = ledger.resolve_commit(repo, commit or "HEAD")
    if sha is None:
        return None, (f"cannot resolve {commit or 'HEAD'!r} in {repo} — an "
                      f"unjoinable key would sit in `unasked` forever")

    def go(tmp):
        parts = os.path.join(tmp, "parts")
        os.makedirs(parts, exist_ok=True)
        report_path = os.path.join(tmp, "results.json")
        setup = os.path.join(tmp, PLUGIN_NAME)
        shutil.copyfile(plugin, setup)
        config = os.path.join(tmp, "tg.vitest.config.mts")
        with open(config, "w") as fh:
            fh.write(config_source(repo, find_config(repo), setup))
        if node_modules and not os.path.exists(os.path.join(tmp, "node_modules")):
            os.symlink(node_modules, os.path.join(tmp, "node_modules"))
        env = dict(os.environ, TGTRACE_OUT_DIR=parts, TGTRACE_ROOT=repo)
        cmd = [os.path.join(node_modules or "", ".bin", "vitest"), "run",
               "--config", config, "--reporter=json", f"--outputFile={report_path}",
               *vitest_args]
        runner = _runner or (lambda: subprocess.run(
            cmd, cwd=repo, env=env, capture_output=True, text=True))
        proc = runner()

        tail = (getattr(proc, "stderr", "") or "").strip()[-400:]
        if not os.path.isfile(report_path):
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
        verdicts = parse_report(report, repo)
        trace, err = merge_parts(parts)
        if err:
            return proc, None, None, err
        if verdicts and not trace["tests"]:
            # Tests ran, the setup file recorded nothing: it did not load.
            return proc, None, None, (
                "vitest ran tests but tgtrace-vitest wrote no trace — the setup "
                "file did not load; no verdict was recorded"
                + (f"\n  vitest stderr (tail): {tail}" if tail else ""))
        return proc, trace, verdicts, None

    if _workdir is not None:
        proc, trace, verdicts, err = go(_workdir)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            proc, trace, verdicts, err = go(tmp)
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
