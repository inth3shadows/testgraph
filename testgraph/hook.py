"""Git-hook entry point: run the selector for a push and say what to test.

This is the consumer the project did not have. `skills/testgraph-verify` was
built as the cheapest one and was invoked zero times across every session on
this machine (issue #49) — a selector nobody calls is a selector nobody can
learn from, and both the results ledger (#10) and trace ground truth (#12)
assume a caller. A `pre-push` hook calls it whether or not anyone remembers to.

Two rules follow from being a hook rather than a command:

1. It NEVER fails a push. Every path returns 0 — a blocked index, a missing
   registry, an unhandled traceback. Advice that can stop a push stops being
   advice; it gets uninstalled, and the invocation count goes back to zero.
2. It prints SHORT. `select._render` emits one NOTE per unparseable entry
   symbol — fourteen of them on signedintake, every push, unchanged forever.
   A reader who learns to skip the block has learned to skip the answer too.
"""
import argparse
import json
import os
import sys
import time

from . import registry as reg
from . import select as sel

MAX_JOURNEYS = 8
MAX_WARNINGS = 3


def state_dir():
    return os.environ.get(
        "TESTGRAPH_STATE_DIR",
        os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "testgraph",
        ),
    )


def log_invocation(record):
    """Append one line to the invocation log; never raise.

    #49's success criterion is a non-zero invocation count after a week of
    normal work, so counting runs IS the deliverable, not telemetry decoration.
    This records that the selector ran and what it answered. It is not the
    results ledger of #10, which records what the journey runs then FOUND —
    different data, and it needs a test runner this has no opinion about."""
    try:
        directory = state_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "invocations.jsonl"), "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def render(result, repo, more_cmd=None):
    """A push-sized summary: the ranked journeys, the reasons the answer might
    be wrong, and nothing that repeats identically on every push."""
    name = reg.repo_name(repo)
    span = f"{_short(result['base'])}..{_short(result['head'])}"

    if result.get("status") == "BLOCKED":
        lines = [f"testgraph[{name}]: no answer for {span} — index not trustworthy"]
        for b in result.get("blocking", []):
            lines.append(f"  x {b}")
        return "\n".join(lines)

    journeys = result.get("journeys", [])
    if not journeys:
        lines = [
            f"testgraph[{name}]: no journeys selected for {span} "
            f"(no product-behavior change detected)"
        ]
    else:
        lines = [
            f"testgraph[{name}]: {len(journeys)} journey(s) this push could break, "
            f"{span}, ranked:"
        ]
        for j in journeys[:MAX_JOURNEYS]:
            flag = "  ! VERIFY MANUALLY" if j.get("verify_manually") else ""
            lines.append(
                f"  [{j['rank']:>3}] {j['id']}  {j['name']}  "
                f"(conf {j['confidence']}){flag}"
            )
        if len(journeys) > MAX_JOURNEYS:
            lines.append(f"  … {len(journeys) - MAX_JOURNEYS} more")

    if result.get("recall_degraded"):
        lines.append("  RECALL DEGRADED — unbounded impact, all journeys listed")
    # Warnings are the channel that carries an unapproved registry and entry
    # drift — both mean "this answer may be understated", so they are worth the
    # lines. Capped: an unbounded warning block is the noise problem again.
    warnings = result.get("warnings", [])
    for w in warnings[:MAX_WARNINGS]:
        lines.append(f"  WARN: {w}")
    if len(warnings) > MAX_WARNINGS:
        lines.append(f"  WARN: … {len(warnings) - MAX_WARNINGS} more")
    if more_cmd:
        lines.append(f"  full: {more_cmd}")
    return "\n".join(lines)


def _short(rev):
    return rev[:9] if len(rev) == 40 and all(c in "0123456789abcdef" for c in rev) else rev


def run(repo, base, head, registry_path=None, caller="pre-push"):
    """Returns (text, record). Raises nothing the caller must handle."""
    started = time.time()
    record = {
        "ts": int(started),
        "repo": reg.repo_name(repo),
        "base": base,
        "head": head,
        "caller": caller,
    }
    registry_path = registry_path or reg.resolve_for_repo(repo)
    if registry_path is None:
        record["status"] = "NO_REGISTRY"
        return (
            f"testgraph[{reg.repo_name(repo)}]: no journey registry for this repo "
            f"— draft one with `python3 -m testgraph.propose --repo {repo}`",
            record,
        )

    db_path = os.path.join(repo, ".codegraph", "codegraph.db")
    if not os.path.exists(db_path):
        record["status"] = "NO_INDEX"
        return (
            f"testgraph[{reg.repo_name(repo)}]: no CodeGraph index at {db_path} "
            f"— build one with `codegraph init {repo}`",
            record,
        )

    result = sel.select(repo, base, head, db_path, registry_path)
    record["status"] = result.get("status")
    record["n_journeys"] = len(result.get("journeys", []))
    record["journey_ids"] = [j["id"] for j in result.get("journeys", [])]
    record["recall_degraded"] = bool(result.get("recall_degraded"))
    record["duration_ms"] = int((time.time() - started) * 1000)
    more = (
        f"python3 -m testgraph.select --repo {repo} --base {base} --head {head}"
    )
    return render(result, repo, more_cmd=more), record


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testgraph.hook")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--caller", default="pre-push")
    args = ap.parse_args(argv)

    try:
        text, record = run(
            args.repo, args.base, args.head, args.registry, caller=args.caller
        )
    except Exception as exc:  # noqa: BLE001 — see rule 1 in the module docstring
        text = f"testgraph[{reg.repo_name(args.repo)}]: skipped — {type(exc).__name__}: {exc}"
        record = {
            "ts": int(time.time()),
            "repo": reg.repo_name(args.repo),
            "base": args.base,
            "head": args.head,
            "caller": args.caller,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
    log_invocation(record)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
