"""Unit tests for `testgraph.propose` and the registry-approval marker (issue #6).

Synthetic fixtures throughout: a proposer that needs a real FastAPI app to be
tested would be untestable in CI and would only ever be checked against
honeyslate, which is the single-repo problem this feature exists to fix.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from testgraph import db as dbmod  # noqa: E402
from testgraph import export as exp  # noqa: E402
from testgraph import propose as prop  # noqa: E402
from testgraph import registry as reg  # noqa: E402

ROUTER = '''\
from fastapi import APIRouter
router = APIRouter()


@router.post("/tasks", status_code=201)
def create_task():
    return 1


@router.get("/tasks/{task_id}")
async def get_task(task_id):
    return task_id


@router.delete("/tasks/{task_id}")
def delete_task(task_id):
    """Present in the source, absent from the hand registry -- the exact
    silent-under-selection gap issue #6 cites."""


@router.get(PREFIX + "/computed")
def computed_path():
    """First decorator arg is not a literal, so the route path is unknown."""


def helper():
    """Undecorated: not an entry point."""


def outer():
    @router.get("/nested")
    def nested():
        """Not importable, so it can never be an entry symbol."""
'''

TEST_MODULE = '''\
from fastapi import APIRouter
router = APIRouter()


@router.get("/fixture")
def fixture_route():
    """A test-only app. Registering it would invent a journey no user reaches."""
'''

BROKEN = "def oops(:\n"


def _repo(tmp):
    files = {
        "app/routers/tasks.py": ROUTER,
        "app/tests/test_api.py": TEST_MODULE,
        "app/broken.py": BROKEN,
        "web/src/App.svelte": "<h1>hi</h1>\n",
        "web/e2e/flow.spec.js": "test()\n",
    }
    for rel, body in files.items():
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
    return tmp


def _db(tmp, present=("create_task", "get_task", "delete_task")):
    """An index that knows `present` and nothing else, plus one high-fan-in
    product symbol and one high-fan-in TEST symbol for the spot-check pick."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT);
        CREATE TABLE unresolved_refs(id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE files(path TEXT, language TEXT, indexed_at INTEGER);
        CREATE TABLE schema_versions(version INT, applied_at INT, note TEXT);
        INSERT INTO schema_versions VALUES (8, 0, 'fixture');
        """
    )
    for name in present:
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
            (f"function:{name}", "function", name, name,
             "app/routers/tasks.py", 1, 2),
        )
    conn.execute(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        ("function:get_db", "function", "get_db", "get_db", "app/deps.py", 1, 2),
    )
    conn.execute(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        ("function:client", "function", "client", "client",
         "app/tests/conftest.py", 1, 2),
    )
    # Source nodes, so an edge's ORIGIN has a file path to filter on.
    for i in range(15):
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
            (f"caller{i}", "function", f"caller{i}", f"caller{i}",
             "app/service.py", 1, 2),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
            (f"tcaller{i}", "function", f"tcaller{i}", f"tcaller{i}",
             "app/tests/test_api.py", 1, 2),
        )
    # `client` is deliberately given MORE inbound edges than `get_db`: without
    # the test-path exclusion it would win the spot-check pick. `get_db`'s 19
    # edges split 15 product / 4 test, so the derived floor and the guard's
    # measurement are distinguishable, and the 15 clear MIN_TOLERANCE_EDGES.
    rows = [(f"caller{i}", "function:get_db", "calls", None, None) for i in range(15)]
    rows += [(f"tcaller{i}", "function:get_db", "calls", None, None) for i in range(4)]
    rows += [(f"tcaller{i % 4}", "function:client", "calls", None, None)
             for i in range(20)]
    conn.executemany(
        "INSERT INTO edges(source, target, kind, metadata, provenance) "
        "VALUES (?,?,?,?,?)", rows,
    )
    conn.commit()
    path = os.path.join(tmp, "cg.db")
    dst = sqlite3.connect(path)
    conn.backup(dst)
    dst.close()
    return path


class ProposeScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _repo(self.tmp)

    def test_module_level_decorated_handlers_only(self):
        hits, unparsed = prop.scan(self.tmp)
        names = {n for _, n, _ in hits}
        self.assertEqual(
            names, {"create_task", "get_task", "delete_task", "computed_path"}
        )
        self.assertNotIn("helper", names)  # undecorated
        self.assertNotIn("nested", names)  # function-local: not importable
        self.assertNotIn("fixture_route", names)  # test module

    def test_unparseable_file_is_skipped_not_raised(self):
        hits, unparsed = prop.scan(self.tmp)
        self.assertEqual(unparsed, ["app/broken.py"])
        self.assertTrue(hits, "one broken module must not sink the whole draft")

    def test_non_literal_route_path_keeps_the_handler(self):
        hits, _ = prop.scan(self.tmp)
        routes = {n: r for _, n, r in hits}
        self.assertEqual(routes["computed_path"], [("GET", None)])
        self.assertEqual(routes["create_task"], [("POST", "/tasks")])


class ProposeDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _repo(self.tmp)
        self.db = _db(self.tmp)
        self.result = prop.propose(self.tmp, self.db, "fixture")
        self.draft = self.result["draft"]

    def test_draft_is_never_born_approved(self):
        self.assertIs(self.draft["approved"], False)
        self.assertEqual(self.draft["proposed_by"], "testgraph.propose")

    def test_one_journey_per_handler_never_merged(self):
        # Splitting is the safe direction; merging two flows behind one id hides
        # which of them broke. The mechanical pass must not guess a boundary.
        self.assertEqual(len(self.draft["journeys"]), 3)
        entries = [
            e["name"]
            for j in self.draft["journeys"].values()
            for e in j["entries"]
        ]
        self.assertEqual(sorted(entries),
                         ["create_task", "delete_task", "get_task"])
        for journey in self.draft["journeys"].values():
            self.assertEqual(len(journey["entries"]), 1)

    def test_unresolvable_handler_is_excluded_and_reported(self):
        # An entry the index cannot resolve makes `registry.unresolved` block the
        # WHOLE registry, so shipping one is worse than omitting it.
        drafted = {
            e["name"]
            for j in self.draft["journeys"].values()
            for e in j["entries"]
        }
        self.assertNotIn("computed_path", drafted)
        excluded = {u["handler"] for u in self.draft["unresolved_candidates"]}
        self.assertEqual(excluded, {"computed_path"})

    def test_drafted_registry_actually_resolves(self):
        conn = dbmod.connect(self.db)
        self.assertEqual(reg.unresolved(conn, self.draft), [],
                         "a draft that blocks select is not a usable draft")
        self.assertEqual(len(reg.resolve_entries(conn, self.draft)), 3)

    def test_journey_ids_are_unique_across_same_path_prefixes(self):
        # GET /tasks/{task_id} and DELETE /tasks/{task_id} slugify identically;
        # ids are file-stem + handler for exactly this reason.
        self.assertEqual(len(set(self.draft["journeys"])),
                         len(self.draft["journeys"]))
        self.assertIn("J_tasks_delete_task", self.draft["journeys"])

    def test_spot_checks_skip_test_files_and_entry_symbols(self):
        checks = self.draft["spot_checks"]
        self.assertIn("get_db", checks)
        self.assertNotIn("client", checks, "test fixtures must not anchor the guard")

    def test_floor_ignores_test_call_sites(self):
        # `get_db` has 19 inbound edges, 4 of them from a test file. The guard
        # later measures ALL 19 via `caller_edge_count`, so a floor derived from
        # the 15 product edges can only err low. Counting the test edges instead
        # would make deleting those tests a BLOCK whose printed remedy
        # (`codegraph index`) can never clear it.
        floor = self.draft["spot_checks"]["get_db"]["min_caller_edges"]
        self.assertEqual(floor, int(15 * prop.SPOT_CHECK_FLOOR))
        conn = dbmod.connect(self.db)
        measured = dbmod.caller_edge_count(conn, "function:get_db")
        self.assertEqual(measured, 19)
        self.assertLess(floor, measured, "the floor must sit below what the guard sees")

    def test_basis_says_when_churn_was_unavailable(self):
        # The fixture is a plain directory, not a git repo.
        self.assertIn("fan-in only", self.draft["spot_check_basis"])

    def test_blind_spots_name_what_the_scan_cannot_see(self):
        joined = " ".join(self.draft["blind_spots"])
        self.assertIn("schedulers", joined)
        self.assertIn("non-Python", joined)
        self.assertIn("App.svelte", joined)
        self.assertNotIn("flow.spec.js", joined, "test files are not product blind spots")
        self.assertIn("do not parse", joined)


class JourneyIdCollisionTests(unittest.TestCase):
    """Two files with the same basename defining the same handler name — the
    `api/v1/users.py` + `api/v2/users.py` shape. File-stem ids collided and the
    journeys dict silently kept only the last, so a handler the proposer FOUND
    never reached the registry."""

    HITS = [
        ("api/v1/users.py", "list_users", [("GET", "/users")]),
        ("api/v2/users.py", "list_users", [("GET", "/users")]),
        ("api/v1/users.py", "get_user", [("GET", "/users/{id}")]),
    ]

    def test_colliding_pairs_widen_to_the_full_path(self):
        ids = prop.assign_ids(self.HITS)
        self.assertEqual(len(set(ids.values())), 3)
        self.assertEqual(ids[("api/v1/users.py", "list_users")],
                         "J_api_v1_users_list_users")
        self.assertEqual(ids[("api/v2/users.py", "list_users")],
                         "J_api_v2_users_list_users")

    def test_non_colliding_ids_stay_short(self):
        self.assertEqual(prop.assign_ids(self.HITS)[("api/v1/users.py", "get_user")],
                         "J_users_get_user")

    def test_every_handler_survives_into_the_draft(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        body = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n\n\n"
            '@router.get("/users")\n'
            "def list_users():\n    return []\n"
        )
        for rel in ("api/v1/users.py", "api/v2/users.py"):
            os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, start_line INT, end_line INT);
            CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
                kind TEXT, metadata TEXT, provenance TEXT);
            CREATE TABLE unresolved_refs(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE files(path TEXT, language TEXT, indexed_at INTEGER);
            CREATE TABLE schema_versions(version INT, applied_at INT, note TEXT);
            INSERT INTO schema_versions VALUES (8, 0, 'fixture');
            """
        )
        for rel in ("api/v1/users.py", "api/v2/users.py"):
            conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                         (f"function:{rel}", "function", "list_users",
                          "list_users", rel, 1, 2))
        conn.commit()
        path = os.path.join(tmp, "cg.db")
        dst = sqlite3.connect(path)
        conn.backup(dst)
        dst.close()

        draft = prop.propose(tmp, path, "collide")["draft"]
        files = {e["file"] for j in draft["journeys"].values() for e in j["entries"]}
        self.assertEqual(files, {"api/v1/users.py", "api/v2/users.py"})


class SpotCheckStabilityTests(unittest.TestCase):
    """Issue #43: fan-in says how depended-upon a symbol is, not how stable that
    number is, and the blocking half of the guard needs the second property."""

    def test_a_narrow_tolerance_band_is_rejected(self):
        # SPOT_CHECK_FLOOR = 0.8 gives a 3-edge symbol a floor of 2, so deleting
        # ONE caller blocks every run. Small fan-in is the most fragile pin there
        # is, whatever its churn -- the failure the first cut of this fix walked
        # straight into by ranking on churn ascending.
        conn = _fanin_db({"tiny": 3, "solid": 20})
        checks, candidates = prop._spot_checks(conn, set(), churn={})
        self.assertIn("solid", checks)
        self.assertNotIn("tiny", checks)
        self.assertNotIn("tiny", {c["name"] for c in candidates})

    def test_volatile_callers_lose_to_a_quieter_symbol(self):
        conn = _fanin_db({"hot": 30, "calm": 20})
        # `hot` has more fan-in but its callers churn hard.
        churn = {"caller_hot_%d.py" % i: 40 for i in range(30)}
        churn.update({"caller_calm_%d.py" % i: 1 for i in range(20)})
        checks, candidates = prop._spot_checks(conn, set(), churn)
        # Both survive into the pool (limit=2 takes two); what matters is which
        # one is picked FIRST, since that is the one a reviewer keeps.
        self.assertEqual(list(checks)[0], "calm")
        self.assertGreater(candidates[0]["score"], candidates[1]["score"])

    def test_quiet_but_obscure_loses_to_quiet_and_load_bearing(self):
        # Ranking on churn alone picked the quietest symbol regardless of how
        # little it asserted: honeyslate went from get_settings (15 of 19 edges)
        # to hash_token (2 of 3). Both quiet here; the load-bearing one must win.
        conn = _fanin_db({"obscure": 12, "loadbearing": 40})
        churn = {"caller_obscure_%d.py" % i: 1 for i in range(12)}
        churn.update({"caller_loadbearing_%d.py" % i: 1 for i in range(40)})
        checks, _ = prop._spot_checks(conn, set(), churn)
        self.assertEqual(list(checks)[0], "loadbearing")

    def test_no_git_history_degrades_to_fan_in_order(self):
        conn = _fanin_db({"big": 40, "small": 12})
        checks, _ = prop._spot_checks(conn, set(), churn={})
        self.assertEqual(list(checks), ["big", "small"])

    def test_churn_is_measured_on_callers_not_the_definition(self):
        # button.tsx is a STABLE file whose callers churn constantly. Scoring on
        # the definition's own history would rank it the safest pin in the repo.
        conn = _fanin_db({"Button": 30})
        callers = {"caller_Button_%d.py" % i: 50 for i in range(30)}
        volatile = prop._spot_checks(conn, set(), {**callers, "def_Button.py": 0})
        quiet = prop._spot_checks(conn, set(), {"def_Button.py": 999})
        self.assertGreater(volatile[1][0]["caller_churn"], 0)
        self.assertEqual(quiet[1][0]["caller_churn"], 0)


def _fanin_db(symbols):
    """In-memory index where each `name: n` gets `n` callers, one per own file."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INT, end_line INT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT, target TEXT,
            kind TEXT, metadata TEXT, provenance TEXT);
        """
    )
    for name, n in symbols.items():
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                     (f"function:{name}", "function", name, name,
                      f"def_{name}.py", 1, 2))
        for i in range(n):
            src = f"caller_{name}_{i}"
            conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                         (src, "function", src, src, f"{src}.py", 1, 2))
            conn.execute(
                "INSERT INTO edges(source, target, kind, metadata, provenance) "
                "VALUES (?,?,?,?,?)", (src, f"function:{name}", "calls", None, None))
    conn.commit()
    return conn


class FileChurnTests(unittest.TestCase):
    def test_counts_commits_per_file_and_survives_a_non_repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(prop.file_churn(tmp), {},
                         "a plain directory must degrade, not raise")

        run = lambda *a: subprocess.run(a, cwd=tmp, check=True, capture_output=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        for i in range(3):
            with open(os.path.join(tmp, "hot.py"), "w") as fh:
                fh.write(f"x = {i}\n")
            run("git", "add", "-A")
            run("git", "commit", "-qm", f"c{i}")
        with open(os.path.join(tmp, "cold.py"), "w") as fh:
            fh.write("y = 1\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "cold")

        churn = prop.file_churn(tmp)
        self.assertEqual(churn["hot.py"], 3)
        self.assertEqual(churn["cold.py"], 1)


class VirtualenvPruningTests(unittest.TestCase):
    """A venv is identified by `pyvenv.cfg`, not by being called `.venv`.

    coriolis-local keeps one at `backend/.uv/wsl-venv/`. Name-based skipping
    missed it, so the scan walked site-packages and drafted 126 third-party
    functions as journey candidates — all correctly excluded as unresolvable,
    but they buried the real exclusions and cost 25s of walk.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        layout = {
            "app/main.py": "def real():\n    pass\n",
            # a venv under a name no skip list would guess
            ".uv/wsl-venv/pyvenv.cfg": "home = /usr\n",
            ".uv/wsl-venv/lib/python3.14/site-packages/dep/mod.py": "def vendored():\n    pass\n",
            # vendored copy with NO marker file — name is the only signal
            "vendor/site-packages/other/mod.py": "def vendored_too():\n    pass\n",
        }
        for rel, body in layout.items():
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)

    def test_marker_file_prunes_a_nonstandard_venv(self):
        found = [os.path.relpath(p, self.tmp) for p in reg.python_sources(self.tmp)]
        self.assertEqual(found, [os.path.join("app", "main.py")])

    def test_unmarked_site_packages_is_pruned_by_name(self):
        found = " ".join(reg.python_sources(self.tmp))
        self.assertNotIn("vendored_too", found)
        self.assertNotIn("site-packages", found)

    def test_prune_dirs_leaves_ordinary_directories(self):
        dirs = ["app", "vendor", ".uv"]
        reg.prune_dirs(self.tmp, dirs)
        self.assertIn("app", dirs)
        # `.uv` itself holds no marker -- only the venv INSIDE it does, so the
        # prune must happen one level down rather than on the parent's name.
        self.assertIn(".uv", dirs)


class EmptyDraftTests(unittest.TestCase):
    """A registry with no journeys is valid, approvable, and answers a confident
    NONE for every change. It must never reach disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "plain.py"), "w") as fh:
            fh.write("def helper():\n    return 1\n")
        self.db = _db(self.tmp, present=())
        self.out = os.path.join(self.tmp, "empty.draft.json")

    def test_nothing_is_written_and_exit_is_nonzero(self):
        code = prop.main(["--repo", self.tmp, "--db", self.db,
                          "--target", "empty", "--out", self.out])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.out))

    def test_bare_out_filename_does_not_crash(self):
        # os.path.dirname("draft.json") is "", and makedirs("") raises
        # FileNotFoundError after the whole scan has already run.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        _repo(self.tmp)
        code = prop.main(["--repo", self.tmp, "--db", _db(self.tmp),
                          "--target", "bare", "--out", "bare.draft.json"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "bare.draft.json")))


class ApprovalMarkerTests(unittest.TestCase):
    def test_approved_registry_is_silent(self):
        self.assertIsNone(reg.approval_warning({"approved": True, "journeys": {}}))

    def test_draft_registry_warns_loudly(self):
        warning = reg.approval_warning(
            {"approved": False, "journeys": {}, "blind_spots": ["a", "b"]}
        )
        self.assertIn("UNAPPROVED REGISTRY", warning)
        self.assertIn("2 declared blind spot", warning)

    def test_missing_marker_is_unknown_provenance_not_approved(self):
        warning = reg.approval_warning({"journeys": {}})
        self.assertIsNotNone(warning)
        self.assertIn("provenance unknown", warning)

    def test_the_remedy_named_in_the_warning_clears_it(self):
        # The failure `unchecked_entries` documents: a permanent warning whose
        # remedy cannot work trains the reader to ignore warnings.
        registry = {"journeys": {}}
        self.assertIsNotNone(reg.approval_warning(registry))
        registry["approved"] = True
        self.assertIsNone(reg.approval_warning(registry))

    def test_map_banner_is_separate_from_the_index_banner(self):
        # Every `warnings` entry renders under "The index was not fully
        # trustworthy", and testgraph-verify reads that banner as "regenerate
        # after `codegraph index`". Approval has no index component, so riding
        # that channel would assert a permanent index fault with a remedy that
        # cannot clear it.
        meta = {
            "repo": "/r", "schema": 8, "commit": "abc123", "symbols": 0,
            "warnings": [], "unchecked_entries": [],
            "registry_approval": reg.approval_warning({"approved": False}),
        }
        md = exp.render_markdown({}, {"journeys": {}}, meta)
        self.assertIn("was not human-approved", md)
        self.assertNotIn("The index was not fully trustworthy", md)
        self.assertIn("Re-indexing does not change this", md)

    def test_shipped_honeyslate_registry_is_marked_approved(self):
        path = os.path.join(ROOT_DIR, "journeys", "honeyslate.json")
        with open(path) as fh:
            self.assertIsNone(reg.approval_warning(json.load(fh)))


if __name__ == "__main__":
    unittest.main()
