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
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(repo, python, tests, root, out, extra_args=(), timeout=1800, env=None):
    """Run the suite under the plugin. Returns (returncode, payload_or_None).

    A non-zero pytest exit is NOT fatal here. A suite with failing tests still
    executed real code, and the traces of the tests that did pass are still the
    truth about what those journeys touch. Refusing to report unless the whole
    suite is green would make the measurement hostage to the target's health."""
    out = os.path.abspath(out)
    env = dict(env if env is not None else os.environ)
    env["TGTRACE_OUT"] = out
    env["TGTRACE_ROOT"] = os.path.abspath(os.path.join(repo, root))
    env["PYTHONPATH"] = os.pathsep.join(
        [HERE] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    cmd = [
        python, "-m", "pytest", os.path.join(repo, tests),
        "-p", "tgtrace", "-p", "no:cacheprovider", "-q",
        *extra_args,
    ]
    proc = subprocess.run(cmd, cwd=repo, env=env, timeout=timeout)
    payload = None
    if os.path.exists(out):
        with open(out) as f:
            payload = json.load(f)
    return proc.returncode, payload


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

    rc, payload = run(
        args.repo, args.python, args.tests, args.root, args.out,
        extra_args=args.pytest_args, timeout=args.timeout,
    )
    if payload is None:
        print(
            f"no trace written to {args.out} — pytest exited {rc} before the plugin "
            f"could report (a collection error, or the plugin was not loaded)",
            file=sys.stderr,
        )
        return 1

    tests = payload["tests"]
    symbols = {tuple(s) for syms in tests.values() for s in syms}
    print(f"traced {len(tests)} test(s), {len(symbols)} distinct symbol(s)")
    print(f"  backend: {payload['backend']}   root: {payload['root']}")
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
