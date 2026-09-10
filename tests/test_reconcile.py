"""Phase 5's two success criteria, from the roadmap's kill criteria (§8):

  1. a call through a module ALIAS is classified STATIC MISS
  2. the `append` shape is classified PHANTOM EDGE

If either fails, §8 says the reconciliation DESIGN is wrong — so these are not
regression tests bolted on after the fact, they are the experiment. The rest of
the file pins the ways the classifier could be right for the wrong reason: an
edge the oracle cannot support but the run executed must NOT read as a phantom,
a shadowed parameter must not hide a legitimate reference, and a run that
traced nothing must not print a miss count at all.
"""
import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph import db as dbmod  # noqa: E402
from testgraph import reconcile as rec  # noqa: E402


# The fixture repo. Every file exists to produce exactly one classification.
SOURCES = {
    "app/__init__.py": "",
    # The callee the resolver fabricates edges to.
    "app/ledger.py": '''
def append(row):
    return True
''',
    # The REAL caller of ledger.append, whose edge CodeGraph loses.
    "app/record.py": '''
from . import ledger


def add_outcome(row):
    return ledger.append(row)
''',
    # #66's shape: `.append` on a call-expression receiver. Nothing here
    # denotes app/ledger.py:append.
    "app/export.py": '''
def build_map(rows):
    out = {}
    out.setdefault("f", []).append(rows)
    return out
''',
    # Reached only through a module ALIAS, which the static graph misses.
    "app/store.py": '''
def resolve_symbol(name):
    return [name]
''',
    # Reached only by getattr dispatch — no source oracle can support it.
    "app/dyn.py": '''
def audit(payload):
    return payload
''',
    "app/handler.py": '''
import importlib

from . import export
from . import store as dbx

HOOK = "audit"


def handle(name):
    export.build_map(name)
    mod = importlib.import_module("app.dyn")
    getattr(mod, HOOK)(name)
    return dbx.resolve_symbol(name)
''',
}

REGISTRY = {
    "target": "demo",
    "approved": True,
    "journeys": {
        "J1": {"name": "handle a thing",
               "entries": [{"name": "handle", "file": "app/handler.py"}]},
    },
}

# What CodeGraph produces for that source, defects included. Each row is
# annotated with the real-world observation it reproduces.
NODES = [
    ("fn:handle", "function", "handle", "app/handler.py", 10),
    ("fn:build_map", "function", "build_map", "app/export.py", 2),
    ("fn:resolve_symbol", "function", "resolve_symbol", "app/store.py", 2),
    ("fn:append", "function", "append", "app/ledger.py", 2),
    ("fn:add_outcome", "function", "add_outcome", "app/record.py", 5),
    ("fn:audit", "function", "audit", "app/dyn.py", 2),
]
EDGES = [
    # Correct: `export.build_map(name)` through a plain module import.
    ("fn:handle", "fn:build_map", "calls", None),
    # FABRICATED (#66): `out.setdefault("f", []).append(rows)` bare-name-matched
    # onto the top-level `append`.
    ("fn:build_map", "fn:append", "calls", None),
    # Synthesized for the getattr dispatch. Real, and unsupportable by source.
    ("fn:handle", "fn:audit", "calls", "heuristic"),
    # ABSENT, and that is the fixture: `dbx.resolve_symbol(name)` through
    # `from . import store as dbx` produces no edge, and neither does
    # `add_outcome`'s plain `ledger.append(row)`.
]

# One test, which ran everything on the J1 path except the ledger write.
TRACE = {
    "root": "/fixture",
    "tests": {
        "tests/test_handle.py::test_handle": [
            ["app/handler.py", "handle"],
            ["app/export.py", "build_map"],
            ["app/store.py", "resolve_symbol"],
            ["app/dyn.py", "audit"],
        ]
    },
}


def build_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT, line INT);
        CREATE TABLE schema_versions(version INT, applied_at INT, note TEXT);
        INSERT INTO schema_versions VALUES (9, 0, 'fixture');
        """
    )
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [(i, k, n, n, f, ln, ln + 3) for i, k, n, f, ln in NODES],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,metadata,provenance,line) "
        "VALUES (?,?,?,?,?,?)",
        [(s, t, k, None, p, 1) for s, t, k, p in EDGES],
    )
    conn.commit()
    return conn


class ReconcileFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = tempfile.mkdtemp(prefix="tg-reconcile-")
        for rel, text in SOURCES.items():
            path = os.path.join(cls.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text.lstrip("\n"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def setUp(self):
        self.conn = build_conn()
        self.report = self._report()

    def _report(self, trace=None):
        conn = build_conn()
        files = rec.file_map(conn)
        per_test, unresolved = rec.trace_nodes(
            conn, (trace or TRACE)["tests"], files
        )
        reach, contained = dbmod.dependency_graph(conn)
        misses = rec.static_misses(conn, REGISTRY, per_test, reach, contained, files)
        findings, counts, _fps = rec.phantom_edges(
            conn, self.repo, REGISTRY, rec.invert(per_test), reach, contained
        )
        return {"misses": misses, "findings": findings, "counts": counts,
                "unresolved": unresolved, "per_test": per_test}

    # --- criterion 1 ---------------------------------------------------------

    def test_alias_call_is_a_static_miss(self):
        """`from . import store as dbx` then `dbx.resolve_symbol(...)` — the
        blind spot measured on testgraph itself, where db.resolve_symbol has
        zero inbound edges and sits in no journey footprint."""
        j1 = self.report["misses"][0]
        self.assertIn("fn:resolve_symbol", j1["misses"])
        self.assertEqual(j1["journey"], "J1")
        self.assertTrue(j1["covering_tests"])

    def test_a_symbol_the_footprint_already_contains_is_not_a_miss(self):
        """`build_map` is reached by a real, resolved edge. If it showed up as
        a miss the finding would be about the join, not the graph."""
        self.assertNotIn("fn:build_map", self.report["misses"][0]["misses"])

    # --- criterion 2 ---------------------------------------------------------

    def test_append_on_a_call_receiver_is_a_phantom_edge(self):
        phantoms = [f for f in self.report["findings"]
                    if f["verdict"] == rec.PHANTOM_CONFIRMED]
        pairs = {(f["source"], f["target"]) for f in phantoms}
        self.assertIn(("fn:build_map", "fn:append"), pairs)

    def test_the_phantom_is_why_the_journey_selects_the_callee(self):
        """The product claim, not just the graph claim: J1 depends on
        `append` ONLY through the fabricated edge."""
        f = next(f for f in self.report["findings"]
                 if (f["source"], f["target"]) == ("fn:build_map", "fn:append"))
        self.assertEqual(f["journeys_losing_callee"], ["J1"])

    def test_a_resolved_module_call_is_not_a_phantom(self):
        """`export.build_map(name)` through `from . import export`. Reporting
        this would make every real edge in the repo an accusation."""
        pairs = {(f["source"], f["target"]) for f in self.report["findings"]}
        self.assertNotIn(("fn:handle", "fn:build_map"), pairs)

    # --- the ways it could be right for the wrong reason ---------------------

    def test_getattr_dispatch_is_an_oracle_gap_not_a_phantom(self):
        """The source oracle cannot support `getattr(mod, HOOK)(name)` and is
        not supposed to be able to. The run executed it, so the edge is real —
        and an oracle that reported it as fabricated would be manufacturing
        exactly the false accusation this design exists to avoid."""
        f = next(f for f in self.report["findings"]
                 if (f["source"], f["target"]) == ("fn:handle", "fn:audit"))
        self.assertEqual(f["verdict"], rec.ORACLE_GAP)
        self.assertEqual(f["runtime"], rec.OBSERVED)

    def test_an_untested_caller_yields_a_weaker_grade_not_a_stronger_one(self):
        """With no trace at all, the phantom is still found by source alone —
        but graded `source-only`, because the suite never contradicted it."""
        report = self._report(trace={"tests": {}})
        f = next(f for f in report["findings"]
                 if (f["source"], f["target"]) == ("fn:build_map", "fn:append"))
        self.assertEqual(f["verdict"], rec.PHANTOM_SOURCE_ONLY)
        self.assertEqual(f["runtime"], rec.UNTESTED)


class SourceOracleTests(unittest.TestCase):
    """The strict oracle in isolation. Its failure direction is the risk: too
    strict and it manufactures phantoms, so every rule that says SUPPORTED is
    pinned here."""

    def _support(self, source, caller, callee_name, callee_file,
                 caller_kind="function"):
        d = tempfile.mkdtemp(prefix="tg-oracle-")
        try:
            path = os.path.join(d, "mod.py")
            with open(path, "w") as fh:
                fh.write(source.lstrip("\n"))
            return rec.source_support(
                d,
                {"name": caller, "file_path": "mod.py", "start_line": None,
                 "kind": caller_kind},
                {"name": callee_name, "file_path": callee_file},
                {},
            )[0]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_shadowing_parameter_does_not_hide_a_real_reference(self):
        """`pytest_adapter.write_outcomes` verbatim in shape: an `append`
        PARAMETER is called at one line and `ledger.append` is named at
        another. Judging per call site calls this a phantom; judging per
        function does not, which is why support is decided per function."""
        self.assertEqual(
            self._support(
                '''
from . import ledger


def write_outcomes(rows, append=None):
    append = append or ledger.append
    for row in rows:
        append(row)
''',
                "write_outcomes", "append", "pkg/ledger.py",
            ),
            rec.SUPPORTED,
        )

    def test_a_bare_call_to_a_shadowed_name_alone_is_not_support(self):
        self.assertEqual(
            self._support(
                '''
def collect(rows, append):
    for row in rows:
        append(row)
''',
                "collect", "append", "pkg/ledger.py",
            ),
            rec.UNSUPPORTED,
        )

    def test_an_aliased_module_call_is_support(self):
        """The alias shape is a CodeGraph blind spot, not a source-level one.
        The oracle must read it, or every aliased call in the repo becomes a
        phantom and the STATIC MISS half is drowned in noise."""
        self.assertEqual(
            self._support(
                '''
from . import db as dbmod


def find(name):
    return dbmod.resolve_symbol(name)
''',
                "find", "resolve_symbol", "pkg/db.py",
            ),
            rec.SUPPORTED,
        )

    def test_a_callback_passed_by_name_is_support(self):
        self.assertEqual(
            self._support(
                '''
from .tasks import reconcile


def schedule(bg, ident):
    bg.add_task(reconcile, ident)
''',
                "schedule", "reconcile", "pkg/tasks.py",
            ),
            rec.SUPPORTED,
        )

    def test_a_method_call_on_self_is_support(self):
        self.assertEqual(
            self._support(
                '''
class Writer:
    def run(self):
        return self.flush()
''',
                "run", "flush", "pkg/writer.py", caller_kind="method",
            ),
            rec.SUPPORTED,
        )

    def test_a_star_import_makes_the_module_unjudgeable(self):
        """Not UNSUPPORTED. A wildcard import can legitimately bind the callee,
        and refusing to follow it is no basis for calling an edge fabricated."""
        self.assertEqual(
            self._support(
                '''
from .ledger import *


def write(row):
    return append(row)
''',
                "write", "append", "pkg/ledger.py",
            ),
            rec.UNKNOWN,
        )

    def test_a_local_list_append_is_not_support(self):
        self.assertEqual(
            self._support(
                '''
def collect(rows):
    out = []
    out.append(rows)
    return out
''',
                "collect", "append", "pkg/ledger.py",
            ),
            rec.UNSUPPORTED,
        )

    def test_a_same_file_bare_call_is_support(self):
        self.assertEqual(
            self._support(
                '''
def append(row):
    return row


def write(row):
    return append(row)
''',
                "write", "append", "mod.py",
            ),
            rec.SUPPORTED,
        )

    def test_a_nested_def_in_the_callers_own_file_is_support(self):
        """Both false phantoms the first run of this oracle produced against
        testgraph itself: `export.py:git` is defined inside `commit_stamp` and
        `select.py:_mark_unmapped` inside `select`. CodeGraph indexes a nested
        def as an ordinary node, so the edge is real — and `_local_names`
        collects nested def names to catch SHADOWING, which made the bare-name
        branch skip them. A nested def is a binding, not a shadow."""
        self.assertEqual(
            self._support(
                '''
import subprocess


def commit_stamp(repo):
    def git(*args):
        return subprocess.run(["git", "-C", repo, *args])

    return git("rev-parse", "HEAD")
''',
                "commit_stamp", "git", "mod.py",
            ),
            rec.SUPPORTED,
        )

    def test_a_nested_def_in_a_different_file_is_still_a_shadow(self):
        """The other half of that rule. A local `def append` cannot be a
        reference to `pkg/ledger.py:append` — telling the two apart is what
        the caller-file check is for."""
        self.assertEqual(
            self._support(
                '''
def collect(rows):
    def append(row):
        return row

    return [append(r) for r in rows]
''',
                "collect", "append", "pkg/ledger.py",
            ),
            rec.UNSUPPORTED,
        )

    def test_a_non_python_callee_is_not_judged(self):
        self.assertEqual(
            self._support(
                '''
def write(row):
    return row
''',
                "write", "append", "pkg/ledger.ts",
            ),
            rec.UNKNOWN,
        )

    def test_a_missing_caller_definition_is_not_judged(self):
        self.assertEqual(
            self._support("def other():\n    return 1\n",
                          "write", "append", "pkg/ledger.py"),
            rec.UNKNOWN,
        )


class HelperTests(unittest.TestCase):
    def test_dotted_refuses_a_call_receiver(self):
        """The one rule the phantom half rests on."""
        call = ast.parse("rows.setdefault('f', []).append(v)").body[0].value
        self.assertIsNone(rec._dotted(call.func.value))

    def test_dotted_reads_a_name_chain(self):
        call = ast.parse("a.b.c.f(v)").body[0].value
        self.assertEqual(rec._dotted(call.func.value), "a.b.c")

    def test_a_named_closure_is_kept_but_a_lambda_is_dropped(self):
        """`runtime_support` cannot observe an edge into a symbol the trace
        discarded, so dropping the whole `<locals>` chain (as
        `harness/ground_truth.py` does) made every edge into a nested def read
        NOT_OBSERVED — and two real ones were reported as phantoms on that
        basis. Anonymous frames stay dropped: the index has no node for
        them."""
        conn = build_conn()
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
            ("fn:git", "function", "git", "git", "app/handler.py", 30, 33),
        )
        conn.commit()
        resolved, unresolved = rec.resolve_traced(
            conn,
            [("app/handler.py", "handle.<locals>.git"),
             ("app/handler.py", "handle.<locals>.<lambda>")],
            rec.file_map(conn),
        )
        self.assertEqual(resolved, {"fn:git"})
        self.assertEqual(unresolved, [])

    def test_path_matches_respects_component_boundaries(self):
        self.assertTrue(rec.path_matches("vendor/app/dyn.py", "app/dyn.py"))
        self.assertTrue(rec.path_matches("app/dyn.py", "app/dyn.py"))
        self.assertFalse(rec.path_matches("myapp/dyn.py", "app/dyn.py"))

    def test_classify_never_accuses_on_an_unreadable_caller(self):
        self.assertIsNone(rec.classify(rec.UNKNOWN, rec.NOT_OBSERVED))
        self.assertIsNone(rec.classify(rec.SUPPORTED, rec.NOT_OBSERVED))

    def test_dropping_an_edge_rebuilds_the_graph_rather_than_subtracting(self):
        """`dependency_graph(drop_edges=...)` must remove the edge, not the
        node: a callee reachable another way has to stay in the footprint."""
        conn = build_conn()
        reach, contained = dbmod.dependency_graph(conn)
        self.assertIn("fn:append", dbmod.footprint({"fn:handle"}, reach, contained))
        clean_reach, clean_contained = dbmod.dependency_graph(
            conn, drop_edges={("fn:build_map", "fn:append")}
        )
        self.assertNotIn(
            "fn:append", dbmod.footprint({"fn:handle"}, clean_reach, clean_contained)
        )
        self.assertIn(
            "fn:build_map",
            dbmod.footprint({"fn:handle"}, clean_reach, clean_contained),
        )


class RenderTests(unittest.TestCase):
    def test_a_run_that_measured_nothing_prints_no_miss_count(self):
        """"0 static misses" over a run that traced no test is the sentence a
        reader quotes, and it is indistinguishable from a clean bill. Same
        refusal `harness/ground_truth.py` makes, and the one issue #75 fixed
        for a NONE printed over a trust warning."""
        out = rec.render({
            "tests_traced": 0,
            "unresolved_symbols": [],
            "static_misses": [],
            "edges": [],
            "edge_counts": {},
            "labels": {},
        })
        self.assertIn("NO MEASUREMENT", out)
        self.assertNotIn("STATIC MISS   0", out)


if __name__ == "__main__":
    unittest.main()
