"""pytest plugin: record every function that actually RAN, per test.

Deliberately standalone — it imports nothing from testgraph and nothing from the
rest of `harness/`. It is loaded into the TARGET repo's interpreter (`pytest -p
tgtrace` with this directory on `PYTHONPATH`), which has its own venv, its own
dependency set, and no reason to be able to import this project.

It lives in its own directory for the same reason. `harness/` contains
`trace.py`, and putting `harness/` on the target's `PYTHONPATH` would SHADOW the
stdlib `trace` module for every import in the traced suite — `import trace` would
resolve here and then fail on `trace.Trace`. Only this directory is exported.

Configured by environment variable rather than pytest options for the same
reason: adding `--tg-*` flags to someone else's pytest run is a change to their
CLI surface, and a target with `addopts` or a strict conftest can reject it.

    TGTRACE_OUT   where to write the JSON result (required, else the plugin is inert)
    TGTRACE_ROOT  only record functions defined under this directory
    TGTRACE_SKIP  colon-separated path fragments to ignore (default: the test dirs)

Output: {"root": ..., "tests": {"<test nodeid>": [["<relpath>", "<qualname>"], ...]}}

Why `sys.monitoring` (PEP 669) when available: it is the low-overhead path, and
the point of tracing a whole suite is that doing so has to stay affordable. It
needs 3.12+, so `sys.setprofile` is kept as the fallback — that keeps this
runnable under testgraph's own 3.11 CI, where the hermetic tests exercise it.
"""
import json
import os
import sys

try:
    import pytest
except ImportError:
    # testgraph is stdlib-only and its CI has no pytest, but the collection
    # logic below is the part that can be wrong. Importable without pytest so
    # `tests/test_ground_truth.py` can drive it directly; the pytest hooks are
    # registered only when pytest is actually present (bottom of the file).
    pytest = None

TOOL_ID = 3  # 0-2 and 5 are reserved (debugger, coverage, profiler, optimizer)

_out_path = os.environ.get("TGTRACE_OUT")
_root = os.path.abspath(os.environ.get("TGTRACE_ROOT", os.getcwd()))
_skip = tuple(
    p for p in os.environ.get("TGTRACE_SKIP", "/tests/:/test_:/.venv/").split(":") if p
)

_current = None          # set() while a test body is running, else None
_results = {}
_seen_files = {}         # abspath -> relpath or None (None = outside root/skipped)


def _relpath(filename):
    """Path relative to the traced root, or None if this file is not ours.

    Memoised because it runs on every function entry in the suite, and the
    answer for a given file never changes within a run.

    The skip list is matched against the RELATIVE path, never the absolute one.
    Matching absolutely means a target checked out under any directory named
    `tests` — `/tmp/pytest-of-user/test_0/repo`, a CI scratch path — skips its
    own entire source tree, and the only symptom is an empty trace that later
    renders as agreement."""
    if filename in _seen_files:
        return _seen_files[filename]
    rel = None
    if filename and not filename.startswith("<"):
        path = os.path.abspath(filename)
        if path.startswith(_root + os.sep):
            candidate = os.path.relpath(path, _root)
            # Leading separator so a fragment like "/tests/" still anchors at
            # the root of the RELATIVE path.
            if not any(s in os.sep + candidate for s in _skip):
                rel = candidate
    _seen_files[filename] = rel
    return rel


def _record(code):
    if _current is None:
        return
    rel = _relpath(getattr(code, "co_filename", None))
    if rel is not None:
        _current.add((rel, getattr(code, "co_qualname", None) or code.co_name))


# --- collection backends -----------------------------------------------------

def _start_monitoring():
    mon = sys.monitoring
    mon.use_tool_id(TOOL_ID, "testgraph-trace")
    mon.register_callback(
        TOOL_ID, mon.events.PY_START, lambda code, offset: _record(code)
    )
    mon.set_events(TOOL_ID, mon.events.PY_START)


def _stop_monitoring():
    mon = sys.monitoring
    mon.set_events(TOOL_ID, 0)
    mon.free_tool_id(TOOL_ID)


def _profile(frame, event, arg):
    if event == "call":
        _record(frame.f_code)


def _start_profile():
    sys.setprofile(_profile)


def _stop_profile():
    sys.setprofile(None)


_HAVE_MONITORING = hasattr(sys, "monitoring")


def _start():
    _start_monitoring() if _HAVE_MONITORING else _start_profile()


def _stop():
    _stop_monitoring() if _HAVE_MONITORING else _stop_profile()


# --- pytest hooks ------------------------------------------------------------

def pytest_configure(config):
    if not _out_path:
        return
    # xdist gives every worker AND the controller its own copy of this module,
    # all writing one path at unconfigure. The controller runs no test body, so
    # its empty `_results` wins the last write and the run reports a green suite
    # with a zero-symbol trace — which `ground_truth.py` then renders as a
    # journey with nothing outside its footprint. Refusing is loud; merging
    # per-worker files is the fix to write when a target actually needs xdist.
    if os.environ.get("PYTEST_XDIST_WORKER") or getattr(
        config.option, "numprocesses", None
    ):
        raise RuntimeError(
            "tgtrace cannot run under pytest-xdist: parallel workers would "
            "overwrite one another's trace and the result would silently be "
            "empty. Re-run without -n/--numprocesses."
        )
    _start()


def pytest_unconfigure(config):
    if not _out_path:
        return
    _stop()
    payload = {
        "root": _root,
        "backend": "sys.monitoring" if _HAVE_MONITORING else "sys.setprofile",
        "tests": {tid: sorted(map(list, syms)) for tid, syms in _results.items()},
    }
    directory = os.path.dirname(os.path.abspath(_out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(_out_path, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)


def pytest_runtest_call(item):
    """Collect only during the test BODY.

    Not setup/teardown: fixtures build the world (a DB, a client, seed rows) and
    everything they touch would land in every journey that shares a fixture,
    which is exactly the over-approximation a trace is supposed to be free of.

    `hookwrapper=True` rather than 8.x's `wrapper=True`: the old spelling is
    still honoured in 8 and is the only one 7 understands, and this plugin runs
    against whatever pytest the TARGET pinned, not one we choose."""
    global _current
    if not _out_path:
        yield
        return
    _current = set()
    try:
        yield
    finally:
        _results.setdefault(item.nodeid, set()).update(_current)
        _current = None


if pytest is not None:
    pytest_runtest_call = pytest.hookimpl(hookwrapper=True)(pytest_runtest_call)
