"""Run a target repo's own test suite under `tgtrace` and keep what RAN.

The instrument half of issue #12. Every recall number this project has published
so far was scored against a STATIC oracle — five hand labels, or
`harness/ast_oracle.py`, which is independent of CodeGraph but still derived from
source text. Both inherit static analysis' blind spots: dynamic dispatch,
`getattr`, decorator registries, framework-driven entry. A trace has none of
them, because it records what executed.

    python3 harness/trace.py --repo <path> --python <venv python> \
        --tests backend/tests --root backend/app --out traces/honeyslate.json

What this deliberately does NOT do
----------------------------------
It does not run the app, seed a database, or reset an environment. #8 decided
testgraph does not own an environment, and that decision holds here: the target's
own test suite is the runnable artifact that already exists, and using it costs
this repo nothing to maintain. The consequence is stated rather than hidden — the
trace sees only what the suite covers, so a journey with no tests traces empty
and is reported as `no_trace`, never as "nothing to select".
"""
import argparse
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from testgraph import pytest_adapter as pa  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# ONLY this directory goes on the target's PYTHONPATH. `harness/` itself holds
# `trace.py`, which would shadow the stdlib `trace` module for every import in
# the traced suite.
PLUGIN_DIR = os.path.join(HERE, "plugin")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _sweep_stale_temps(out):
    """Remove `<out>.tmp.<pid>` files whose writer is dead.

    A run SIGKILLed mid-dump leaves tgtrace's temp behind (#154) and nothing
    else removes it. Only `.tmp.<digits>` names, and only dead pids: a broader
    glob took unrelated files (`a.json.tmp.json`) and a CONCURRENT run's
    in-progress temp, whose rename then failed (#154 review F6)."""
    name = re.compile(re.escape(os.path.basename(out)) + r"\.tmp\.(\d+)$")
    directory = os.path.dirname(out)
    try:
        siblings = os.listdir(directory)
    except OSError:
        return
    for sib in siblings:
        m = name.match(sib)
        if m and not _pid_alive(int(m.group(1))):
            try:
                os.remove(os.path.join(directory, sib))
            except OSError:
                pass


def run(repo, python, tests, root, out, extra_args=(), timeout=1800, env=None):
    """Run the suite under the plugin. Returns (returncode, payload_or_None).

    A non-zero pytest exit is NOT fatal here. A suite with failing tests still
    executed real code, and the traces of the tests that did pass are still the
    truth about what those journeys touch. Refusing to report unless the whole
    suite is green would make the measurement hostage to the target's health.

    A pytest that dies BEFORE the plugin can report is different, and the
    difference has to survive: the output file is deleted first, so "the file
    exists" means "this run wrote it". Without that, a run killed at its timeout
    — the exact honeyslate case — would load the PREVIOUS run's trace and report
    it as fresh."""
    repo = os.path.abspath(repo)
    if pa.not_a_file_target(out):
        # This runner deletes `out` first and reads it back after, so a stream
        # can be neither: deleting a FIFO removes it from under its reader,
        # and /dev/stdout cannot be unlinked at all (#154 review F5). The rule
        # is the adapter's, imported, so it cannot drift into a third copy
        # (R2-4, R2-8).
        raise ValueError(f"--out {out} is not a regular file path; this "
                         f"runner reads the trace back, so it needs one")
    out = os.path.abspath(out)
    if os.path.exists(out):
        os.remove(out)
    _sweep_stale_temps(out)
    env = dict(env if env is not None else os.environ)
    env["TGTRACE_OUT"] = out
    env["TGTRACE_ROOT"] = os.path.join(repo, root)
    env["PYTHONPATH"] = os.pathsep.join(
        [PLUGIN_DIR] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    cmd = [
        python, "-m", "pytest", os.path.join(repo, tests),
        "-p", "tgtrace", "-p", "no:cacheprovider", "-q",
        *extra_args,
    ]
    try:
        rc = subprocess.run(cmd, cwd=repo, env=env, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        # The documented honeyslate failure. Report it as the no-trace case the
        # caller already handles, rather than as a traceback.
        return None, None
    payload = None
    if os.path.exists(out):
        # tgtrace writes atomically, but an OS crash or a hand-edited file can
        # still leave something unreadable. That is the no-trace case the
        # caller already handles, not a traceback (#154).
        try:
            with open(out) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict) or pa.payload_shape_error(payload):
            payload = None
    return rc, payload


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness/trace.py")
    ap.add_argument("--repo", required=True, help="target repo root")
    ap.add_argument("--python", required=True, help="the target's interpreter (its venv)")
    ap.add_argument("--tests", default="tests", help="test path, relative to --repo")
    ap.add_argument("--root", default=".", help="source root to record, relative to --repo")
    ap.add_argument("--out", required=True, help="where to write the trace JSON")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("pytest_args", nargs="*", help="extra args passed to pytest")
    args = ap.parse_args(argv)

    try:
        rc, payload = run(
            args.repo, args.python, args.tests, args.root, args.out,
            extra_args=args.pytest_args, timeout=args.timeout,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if payload is None:
        why = (
            f"timed out after {args.timeout}s"
            if rc is None
            else f"exited {rc} with no readable trace "
            f"(a collection error, the plugin was not loaded, or the file is "
            f"truncated or not a tgtrace object)"
        )
        print(f"no trace written to {args.out} — pytest {why}", file=sys.stderr)
        return 1

    tests = payload["tests"]
    symbols = {tuple(s) for syms in tests.values() for s in syms}
    if not symbols:
        # A green run that recorded nothing is the worst output this can produce,
        # because everything downstream reads it as "no journey has a problem".
        print(
            f"traced {len(tests)} test(s) and ZERO symbols — --root "
            f"({args.root}) is almost certainly wrong, or every source file was "
            f"skipped. Do NOT score this trace.",
            file=sys.stderr,
        )
        return 1
    print(f"traced {len(tests)} test(s), {len(symbols)} distinct symbol(s)")
    print(f"  backend: {payload.get('backend', '?')}   root: {payload.get('root', '?')}")
    print(f"  wrote {args.out}")
    empty = [t for t, s in tests.items() if not s]
    if empty:
        # A test that recorded nothing exercised no code under --root. Usually
        # the root is wrong; occasionally the test really is a pure assertion.
        print(f"  {len(empty)} test(s) recorded no symbol under the traced root")
    if rc != 0:
        print(f"  note: pytest exited {rc} — traces of the tests that ran still stand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
