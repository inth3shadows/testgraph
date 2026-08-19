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
import os
import sys
import time

from . import ledger
from . import registry as reg
from . import select as sel

MAX_JOURNEYS = 8
MAX_WARNINGS = 3
MAX_CONFINED = 3

# Kept as an alias: `ledger` now owns where state lives, but this name is the
# one the tests and the docs already point at.
state_dir = ledger.state_dir


def log_invocation(record):
    """Append this run to the ledger as a `selection` row; never raise.

    #49's success criterion is a non-zero invocation count after a week of
    normal work, so counting runs IS the deliverable, not telemetry decoration.
    That count now lives in `ledger.jsonl` alongside the `outcome` rows written
    by `testgraph.record` — one store, because the number the project actually
    wants ("a journey failed and the selection did not name it") is a JOIN
    across the two kinds, and a join across two files is a join nobody runs."""
    ledger.append(ledger.selection_row(record))


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
    # Like recall_degraded above, this gets its own line rather than riding
    # the warnings channel below: capped at MAX_WARNINGS, a push with an
    # unapproved-registry warning plus entry drift already queued ahead of it
    # would silently swallow the one signal issue #63 exists to surface. But
    # NEVER just swallowed, not the same as UNCAPPED IN LENGTH: a wide
    # rename/refactor confining many files would otherwise print one
    # unbroken multi-hundred-character line, the exact noise rule 2 in this
    # module's docstring exists to prevent.
    confined = result.get("closure_confined", [])
    if confined:
        shown = ", ".join(confined[:MAX_CONFINED])
        if len(confined) > MAX_CONFINED:
            shown += f", … {len(confined) - MAX_CONFINED} more"
        lines.append(
            f"  NOTE: impact for {shown} did not leave the file it started "
            f"in — that file's own contribution is UNKNOWN, not verified-safe"
        )
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
        # The join keys against `outcome` rows. `head`/`base` are whatever the
        # caller said — git hands the hook full shas, a human types "HEAD" and
        # "HEAD~1" — and two spellings of one commit join to nothing.
        #
        # `base_commit` is not decoration. The baseline check in
        # `ledger.summarize` looks up (base, journey) in outcomes keyed on
        # RESOLVED shas, so a symbolic base matches nothing and every failure on
        # that push falls to `unbaselined` — pinning observed_recall at None for
        # good, for any caller that passes a symbolic base. `head` got this
        # treatment from the start; `base` did not, and only started mattering
        # once the baseline gated the score.
        "commit": ledger.resolve_commit(repo, head),
        "base_commit": ledger.resolve_commit(repo, base),
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
    record["closure_confined"] = result.get("closure_confined", [])
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
            "commit": ledger.resolve_commit(args.repo, args.head),
            "caller": args.caller,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
    log_invocation(record)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
