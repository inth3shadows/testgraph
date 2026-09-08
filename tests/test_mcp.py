"""The MCP stdio server (Phase 1).

Two things carry this feature and only one of them is about MCP.

The protocol half is ordinary: frame JSON-RPC correctly, never answer a
notification, and survive garbage without dying — a server that exits takes the
tool out of the session with it.

The other half is the reason the server is hand-rolled at all. MCP servers are
spawned once per Claude Code session, and `claudew` opens a session per
worktree, so a server's IDLE footprint is multiplied by every open shell. The
box this was written on measured 3369 MB across 64 MCP processes and has been
wedged twice by memory exhaustion. `test_idle_rss_stays_under_budget` and
`test_no_heavy_imports_at_module_scope` are that constraint written down: they
fail the first time someone hoists a convenience import to module scope, which
is the only way this regresses.
"""
import json
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from testgraph import mcp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The build gate from ~/.claude/plans/testgraph-verification-direction.md §12.4:
# above this, a plain CLI + skill (which costs zero resident bytes when idle)
# is the better answer and the server should not exist. Measured headroom over
# the observed footprint is deliberate but not generous — the point is to catch
# a pydantic-shaped regression, not to leave room for one.
IDLE_RSS_BUDGET_KB = 30 * 1024

# Imports that drag in real weight and that an IDLE server has no use for:
# sqlite3 and subprocess arrive with select+db, argparse with careless CLI code.
# `re` is NOT here despite being pulled by select — a bare `python3 -c` already
# has it loaded via site/encodings, so it is not attributable to this module and
# asserting on it only produces a test that fails on arrival.
FORBIDDEN_IDLE_MODULES = {"sqlite3", "subprocess", "argparse"}


def _request(method, msg_id=1, **params):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        msg["params"] = params
    return msg


class Protocol(unittest.TestCase):
    def test_initialize_echoes_the_clients_protocol_version(self):
        """Agreeing with the client beats asserting our own.

        This server implements only `tools`, whose shape is stable across the
        versions in the wild, so echoing avoids a version disagreement that
        would cost a reconnect for no behavioural difference.
        """
        res = mcp.handle(_request("initialize", protocolVersion="2024-11-05"))
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(res["result"]["capabilities"], {"tools": {}})
        self.assertEqual(res["result"]["serverInfo"]["name"], "testgraph")

    def test_initialize_falls_back_when_the_client_names_no_version(self):
        res = mcp.handle(_request("initialize"))
        self.assertEqual(res["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_notifications_are_never_answered(self):
        """A reply to `notifications/initialized` is fatal for some clients."""
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_advertises_both_tools_with_schemas(self):
        res = mcp.handle(_request("tools/list"))
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertEqual(names, {"testgraph_impact", "testgraph_journeys"})
        for tool in res["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("description", tool)

    def test_unknown_method_is_a_protocol_error_not_a_tool_error(self):
        res = mcp.handle(_request("resources/list"))
        self.assertEqual(res["error"]["code"], mcp.METHOD_NOT_FOUND)

    def test_ping_answers(self):
        self.assertEqual(mcp.handle(_request("ping"))["result"], {})


class Loop(unittest.TestCase):
    def _run(self, lines):
        out = io.StringIO()
        mcp.serve(stdin=io.StringIO("".join(l + "\n" for l in lines)), stdout=out)
        return [json.loads(l) for l in out.getvalue().splitlines() if l]

    def test_one_json_object_per_line(self):
        got = self._run([json.dumps(_request("ping", msg_id=i)) for i in (1, 2, 3)])
        self.assertEqual([r["id"] for r in got], [1, 2, 3])

    def test_malformed_json_does_not_kill_the_loop(self):
        """A dead server takes the session's tool with it, silently."""
        got = self._run(["{not json", json.dumps(_request("ping", msg_id=7))])
        self.assertEqual(got[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(got[1]["id"], 7)

    def test_non_object_request_is_rejected_without_dying(self):
        got = self._run(["[1,2,3]", json.dumps(_request("ping", msg_id=9))])
        self.assertEqual(got[0]["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(got[1]["id"], 9)

    def test_blank_lines_are_skipped(self):
        self.assertEqual(self._run(["", json.dumps(_request("ping"))])[0]["id"], 1)


class ToolResults(unittest.TestCase):
    def test_a_failing_tool_returns_iserror_not_a_protocol_error(self):
        """The model must be able to READ the failure and adapt.

        A JSON-RPC error is invisible to it, so a tool that ran and failed has
        to come back as a normal result with isError set.
        """
        res = mcp.handle(_request("tools/call", name="nope", arguments={}))
        self.assertNotIn("error", res)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("nope", res["result"]["content"][0]["text"])

    def test_an_exception_in_a_handler_becomes_a_tool_result(self):
        with mock.patch.dict(mcp.HANDLERS, {"boom": mock.Mock(side_effect=RuntimeError("x"))}):
            text, is_error = mcp.call_tool("boom", {})
        self.assertTrue(is_error)
        self.assertIn("RuntimeError", text)

    def test_stray_stdout_from_a_handler_cannot_desynchronise_the_stream(self):
        """This file owns stdout as a framing channel.

        One `print` anywhere downstream would interleave with the JSON-RPC
        stream and desynchronise the client rather than fail loudly, so the
        handler runs with stdout redirected.
        """
        def noisy(_args):
            print("this would corrupt the protocol")
            return "ok", False

        out = io.StringIO()
        with mock.patch.dict(mcp.HANDLERS, {"noisy": noisy}), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            mcp.serve(
                stdin=io.StringIO(
                    json.dumps(_request("tools/call", name="noisy", arguments={})) + "\n"
                ),
                stdout=out,
            )
        lines = [l for l in out.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"]["content"][0]["text"], "ok")


class NoRegistry(unittest.TestCase):
    def test_a_missing_registry_is_not_reported_as_no_journeys_affected(self):
        """The distinction this whole tool exists to preserve.

        `resolve_for_repo` returning None means testgraph has nothing registered
        here. An agent that reads that as "nothing is affected" gets silence and
        calls it safety — the exact failure journeys/testgraph.json calls
        catastrophic.
        """
        for tool in ("testgraph_impact", "testgraph_journeys"):
            with self.subTest(tool=tool):
                text, is_error = mcp.call_tool(tool, {"repo": "/nonexistent/repo-xyz"})
                self.assertTrue(is_error)
                self.assertIn("no journey registry found", text)
                self.assertIn("NOT", text)


class WarningCap(unittest.TestCase):
    def _impact_with(self, result):
        with mock.patch("testgraph.registry.resolve_for_repo", return_value="/r.json"), \
                mock.patch("os.path.exists", return_value=True), \
                mock.patch("testgraph.select.select", return_value=result):
            text, is_error = mcp.call_tool("testgraph_impact", {"repo": "/repo"})
        return json.loads(text), is_error

    def test_warnings_are_capped_and_the_omission_is_stated(self):
        payload, _ = self._impact_with(
            {"status": "OK", "journeys": [], "warnings": [f"w{i}" for i in range(30)]}
        )
        self.assertEqual(len(payload["warnings"]), mcp.MAX_WARNINGS + 1)
        self.assertIn("18 more warning(s)", payload["warnings"][-1])

    def test_the_journey_list_is_never_truncated(self):
        """Capping journeys would be silent under-selection.

        Warnings are free text and can be dropped; the journey list IS the
        answer. hook.py caps warnings for the same reason and caps nothing else.
        """
        journeys = [{"id": f"J{i}", "name": f"j{i}"} for i in range(200)]
        payload, _ = self._impact_with({"status": "OK", "journeys": journeys, "warnings": []})
        self.assertEqual(len(payload["journeys"]), 200)

    def test_blocked_status_surfaces_as_a_tool_error(self):
        """BLOCKED means the index is not trustworthy.

        Returning it as an ordinary success would let the model read the
        journey list as an answer when select() is telling it not to.
        """
        _, is_error = self._impact_with({"status": "BLOCKED", "journeys": [], "warnings": []})
        self.assertTrue(is_error)


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class IdleCost(unittest.TestCase):
    """The constraint that made this file hand-rolled instead of an SDK wrapper."""

    def test_no_heavy_imports_at_module_scope(self):
        """Rule 2: select/db/registry are imported inside the call handler.

        Hoisting any of them to module scope is invisible in every other test —
        the server still works, it just costs sqlite3 and subprocess in every
        idle worktree. This is the only place that notices.
        """
        probe = (
            "import sys, json; import testgraph.mcp; "
            "print(json.dumps(sorted(set(sys.modules) & %r)))" % (FORBIDDEN_IDLE_MODULES,)
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(json.loads(out), [], "an idle server imported these; keep them lazy")

    def test_idle_rss_stays_under_budget(self):
        """Measured after a full handshake, which is the steady state.

        A server sits idle in every worktree Claude Code has open, so this
        number is multiplied by the number of shells, not paid once.
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "testgraph.mcp"],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        try:
            proc.stdin.write(json.dumps(_request("initialize", protocolVersion="2025-06-18")) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.write(json.dumps(_request("tools/list", msg_id=2)) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()

            with open(f"/proc/{proc.pid}/status") as fh:
                rss_kb = next(
                    int(l.split()[1]) for l in fh if l.startswith("VmRSS:")
                )
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()

        self.assertLess(
            rss_kb, IDLE_RSS_BUDGET_KB,
            f"idle RSS {rss_kb / 1024:.1f} MB exceeds the "
            f"{IDLE_RSS_BUDGET_KB / 1024:.0f} MB budget; at this size a plain "
            f"CLI + skill is the better design (plan §12.4)",
        )


if __name__ == "__main__":
    unittest.main()
