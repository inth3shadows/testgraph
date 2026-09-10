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

Output: {"root": ..., "tests": {"<test nodeid>": [["<relpath>", "<qualname>"], ...]},
         "declared": {"<test nodeid>": ["J2", ...]}}

`declared` records which journeys each test CLAIMS, read from a plain
`tg_journeys` attribute (`testgraph.results.covers` sets it). Read by NAME, not
by importing that function: this plugin loads into the target repo's
interpreter and imports nothing from testgraph, which is the property that lets
it run against a repo that has never heard of this project. The claim is also
turned into a real `tg_J*` pytest marker at collection time, so
`pytest -m 'tg_J1 or tg_J2'` works natively and `testgraph.verify` needs no
stored attribution map.

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
_declared = {}           # nodeid -> [journey id, ...] the test CLAIMS to cover

# The attribute `testgraph.results.covers` sets. Duplicated as a literal rather
# than imported, deliberately -- see the module docstring on standalone-ness.
JOURNEY_ATTR = "tg_journeys"
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
        "declared": {tid: list(j) for tid, j in sorted(_declared.items())},
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


def _declared_for(item):
    """Journeys `item` claims, from the test function then its class.

    Both levels, because a whole TestCase class exercising one journey is the
    common shape and repeating the decorator on every method invites the two to
    drift apart. Method and class declarations UNION rather than override: a
    method that adds a journey to its class's claim is adding a claim, not
    replacing one."""
    found = []
    for holder in (getattr(item, "function", None), getattr(item, "cls", None)):
        for jid in (getattr(holder, JOURNEY_ATTR, ()) or ()):
            if jid not in found:
                found.append(jid)
    return found


def pytest_collection_modifyitems(config, items):
    """Turn `tg_journeys` into real `tg_J*` markers so `-m` can select on them.

    This is the whole reason `verify` needs no stored attribution map: pytest
    owns discovery, and a map that is not stored cannot go stale.

    Registering the marker as it is added keeps `--strict-markers` runs and the
    unknown-mark warning quiet without asking the target repo to edit its own
    pytest config -- the same reasoning as configuring this plugin by
    environment variable instead of adding CLI flags to someone else's run."""
    for item in items:
        for jid in _declared_for(item):
            name = f"tg_{jid}"
            config.addinivalue_line("markers", f"{name}: testgraph journey {jid}")
            item.add_marker(getattr(pytest.mark, name))
        _declared[item.nodeid] = _declared_for(item)


if pytest is not None:
    pytest_runtest_call = pytest.hookimpl(hookwrapper=True)(pytest_runtest_call)
