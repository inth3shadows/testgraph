"""MCP stdio server — exposes journey impact to a coding agent as a tool.

Phase 1 of ~/.claude/plans/testgraph-verification-direction.md: make something
call the engine unprompted in the agent loop, before building execution on top
of it.

WHY THIS IS HAND-ROLLED AND NOT THE `mcp` SDK. Measured on the dev box
2026-09-08 at an idle moment: 3369 MB resident across 64 MCP-ish processes —
Python MCP-SDK servers sit at 62-69 MB each, and most of that is pydantic +
anyio + httpx, not the server. MCP stdio servers are spawned ONE PER CLAUDE CODE
SESSION, and the `claudew` workflow opens a session per worktree, so a 65 MB
server costs 65 MB times every open shell whether or not it is ever called. The
same box has a memory history: it wedged twice on 2026-07-12 (~40 min apart;
existing processes limped, new `wsl.exe` sessions returned
Wsl/Service/0x8007274c, tmux hung), which is why .wslconfig now pins
memory=24GB + swap=8GB, and the guest OOM-killer then fired twice on 2026-08-26.

Be precise about which of those this file addresses. The Aug 26 kills were a
SINGLE runaway process each time (python3 at 18.3 GB anon-rss, then python at
18.9 GB), not accumulation — so a fleet of small servers is the CHRONIC
pressure, not the acute cause. This server is cheap for the chronic half. The
acute half was checked separately and is not reachable from here: the impact
closure is a recursive CTE evaluated inside SQLite and bounded by node count, so
seeding every node of the largest index on the box (17,229 nodes / 65,360 edges)
peaks at 36 MB and frees on connection close.

MCP stdio is newline-delimited JSON-RPC 2.0. That is a small enough protocol to
implement against directly, and doing so keeps testgraph's existing
zero-runtime-dependency property (see pyproject.toml) rather than spending it on
a transport. The rules that keep the idle cost near a bare interpreter, in
priority order:

  1. stdlib only — no SDK, no third-party import anywhere in this file.
  2. LAZY IMPORTS — select/db/registry/integrity are imported inside the call
     handler. An idle server has never imported sqlite3, re, or subprocess.
  3. No resident index — the SQLite connection is opened per call and closed in
     a finally. The graph stays on disk.
  4. No concurrency — a synchronous blocking read loop. No threads, no asyncio.
  5. Bounded responses, but NEVER a truncated journey list. See MAX_WARNINGS.
  6. Register per-repo, not globally. Global registration is precisely what
     produced 64 processes.

tests/test_mcp.py pins (1) and (2) with a measured idle-RSS assertion, because
otherwise the first convenience import at module scope silently undoes them.
"""
import contextlib
import json
import sys

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "testgraph"

# Warnings are free text and can run long on a stale index; the journey list is
# the answer and is NEVER capped. Capping it would be silent under-selection —
# the one failure mode this whole project is built to avoid (see
# journeys/testgraph.json). hook.py caps warnings for the same reason and calls
# it MAX_WARNINGS there too.
MAX_WARNINGS = 12

# JSON-RPC 2.0 reserved codes. Tool-level failures do NOT use these: per MCP,
# a tool that ran and failed returns isError=true in the RESULT, so the model
# can read the message and adapt. A protocol error is not visible to the model.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

TOOLS = [
    {
        "name": "testgraph_impact",
        "description": (
            "Given a git diff in a repo with a journey registry, name the user "
            "journeys the change could have broken, ranked, with a confidence "
            "per journey. Recall-first: it over-selects rather than miss one, "
            "and degrades toward listing everything when the index cannot be "
            "trusted. Use before committing or reporting work complete. "
            "`base` defaults to HEAD~1 (the last commit); pass the base branch "
            "(e.g. origin/main) to scope it to a whole branch instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo working tree. Defaults to the server's cwd.",
                },
                "base": {"type": "string", "description": "Base rev. Default HEAD~1."},
                "head": {"type": "string", "description": "Head rev. Default HEAD."},
            },
        },
    },
    {
        "name": "testgraph_journeys",
        "description": (
            "List the user journeys registered for a repo, with the entry "
            "symbols and route each one is anchored to. Answers 'what "
            "behaviors does this project claim to have' without running a diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo working tree. Defaults to the server's cwd.",
                }
            },
        },
    },
]


def _no_registry_message(reg, repo):
    """The same refusal `select.main` prints, shaped for a tool result.

    Deliberately not softened to an empty list: `resolve_for_repo` returning
    None means the repo has no registry, which is NOT the same answer as "no
    journeys are affected", and an agent that cannot tell those apart will read
    silence as safety.
    """
    name = reg.repo_name(repo)
    return (
        f"no journey registry found for repo `{name}` ({repo}). This is NOT "
        f"'no journeys affected' — testgraph has nothing registered here and "
        f"cannot answer. Add journeys/<name>.json with \"target\": \"{name}\", "
        f"or draft one with `python3 -m testgraph.propose --repo {repo}`."
    )


def _tool_impact(args):
    # Lazy by design — see the module docstring, rule 2. Importing these at
    # module scope would put sqlite3, re and subprocess in every idle server.
    import os

    from . import registry as reg
    from . import select as sel

    repo = args.get("repo") or os.getcwd()
    base = args.get("base") or "HEAD~1"
    head = args.get("head") or "HEAD"

    registry_path = reg.resolve_for_repo(repo)
    if registry_path is None:
        return _no_registry_message(reg, repo), True

    db_path = os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.exists(db_path):
        return (
            f"no CodeGraph index at {db_path} — testgraph reads an existing "
            f"index and does not build one. Run `codegraph init` in {repo}.",
            True,
        )

    result = sel.select(repo, base, head, db_path, registry_path)

    warnings = result.get("warnings") or []
    if len(warnings) > MAX_WARNINGS:
        extra = len(warnings) - MAX_WARNINGS
        result["warnings"] = warnings[:MAX_WARNINGS] + [
            f"... and {extra} more warning(s), omitted from this response only"
        ]
    return json.dumps(result, indent=2), result.get("status") == "BLOCKED"


def _tool_journeys(args):
    import os

    from . import registry as reg

    repo = args.get("repo") or os.getcwd()
    registry_path = reg.resolve_for_repo(repo)
    if registry_path is None:
        return _no_registry_message(reg, repo), True

    registry = reg.load(registry_path)
    journeys = registry.get("journeys", {})
    out = {
        "target": registry.get("target"),
        "registry": registry_path,
        # An unapproved registry is runnable on purpose (issue #6), but a NONE
        # computed from one means "not registered", not "not affected". select()
        # surfaces this as a warning; surface it here too rather than let a
        # journey list look more settled than it is.
        "approved": bool(registry.get("approved")),
        "journeys": [
            {
                "id": jid,
                "name": reg.journey_name(registry, jid),
                "entries": journeys[jid].get("entries", []),
                "route": journeys[jid].get("route", []),
            }
            for jid in sorted(journeys, key=reg.journey_sort_key)
        ],
    }
    return json.dumps(out, indent=2), False


HANDLERS = {"testgraph_impact": _tool_impact, "testgraph_journeys": _tool_journeys}


def call_tool(name, args):
    """Run one tool. Returns (text, is_error); never raises.

    A traceback reaching the read loop would kill the server and take the
    session's tool with it, so every failure is converted into a tool result the
    model can read. stdout is redirected to stderr for the duration: this file
    owns stdout as a JSON-RPC framing channel, and a single stray print from
    anything downstream would desynchronise the stream rather than fail loudly.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return f"unknown tool `{name}`", True
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return handler(args or {})
    except Exception as exc:  # noqa: BLE001 - see docstring
        return f"{type(exc).__name__}: {exc}", True


def handle(msg):
    """Map one JSON-RPC request to a response, or None for a notification."""
    method = msg.get("method")
    msg_id = msg.get("id")
    # A notification has no `id` and MUST NOT be answered. `notifications/
    # initialized` is the one every client sends; replying to it is a protocol
    # violation that some clients treat as fatal.
    if msg_id is None:
        return None

    if method == "initialize":
        # Echo the client's protocol version when it names one. This server
        # implements only `tools`, whose shape has not changed across the
        # versions in the wild, so agreeing with the client is safer than
        # asserting our own and being told to reconnect.
        requested = (msg.get("params") or {}).get("protocolVersion")
        from . import __version__

        return _ok(
            msg_id,
            {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        )
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        text, is_error = call_tool(params.get("name"), params.get("arguments"))
        return _ok(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )
    if method == "ping":
        return _ok(msg_id, {})
    return _err(msg_id, METHOD_NOT_FOUND, f"unknown method `{method}`")


def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None):
    """Blocking newline-delimited JSON-RPC loop. Returns on EOF.

    Synchronous on purpose (rule 4). One agent session issues one tool call at a
    time, so a thread pool would buy nothing and cost a resident interpreter
    thread per session.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            # No id is recoverable from unparseable input, so this reply is
            # addressed to null per JSON-RPC 2.0.
            _write(stdout, _err(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            continue
        if not isinstance(msg, dict):
            _write(stdout, _err(None, INVALID_REQUEST, "request must be an object"))
            continue
        try:
            response = handle(msg)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any one request
            response = _err(msg.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        if response is not None:
            _write(stdout, response)
    return 0


def _write(stdout, payload):
    # One message per line, and flush every time: the client is blocked reading
    # this stream, and Python line-buffers stdout only when it is a tty. Under
    # MCP it is a pipe, so without the flush the first response can sit in the
    # buffer until the process exits.
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        # Not argparse: importing it costs memory in the common path (rule 2)
        # to serve a flag nothing in the MCP handshake ever passes.
        sys.stderr.write(
            "usage: python3 -m testgraph.mcp\n\n"
            "MCP stdio server. Speaks newline-delimited JSON-RPC 2.0 on "
            "stdin/stdout;\nnot meant to be run interactively. Register it in a "
            "repo's .mcp.json:\n\n"
            '  {"mcpServers": {"testgraph": {"command": "python3",\n'
            '                                "args": ["-m", "testgraph.mcp"]}}}\n\n'
            "Register PER-REPO, not globally: MCP servers are spawned once per\n"
            "Claude Code session, so a global entry runs in every worktree.\n"
        )
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
