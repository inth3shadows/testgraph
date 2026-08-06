"""Append-only record of what testgraph ANSWERED and what the journey runs FOUND.

Two row kinds live in one file, discriminated by `kind`:

    selection — testgraph named these journeys for this commit (written by the
                pre-push hook, and by any other caller of `hook.run`)
    outcome   — a journey was actually run at this commit and passed/failed
                (written by `testgraph.record`, by a human or by /autorun)

Neither kind says anything alone. A selection log answers "was the tool called";
an outcome log answers "did the app break". Joined on `(repo, commit)` they
answer the only question the project has ever asserted rather than measured:
**did a journey fail on a commit whose selection did not name it** — a silent
under-selection, the one failure mode a recall-first selector must not have.

Storage — why a local JSONL and not the KB
------------------------------------------
Issue #10 decided this ledger belongs in the shared `kb.*` Postgres, not a local
store. That decision is honoured in *intent* by `record --export-kb`, and
declined as the write path, because a stdlib CLI cannot be the writer:

  - the KB is reachable only through an MCP server that an AGENT drives; there
    is no Python client here, and adding one means a network dependency, a
    tunnel and a credential inside a hook whose entire contract is that it never
    fails a push. Advice that can stop a push stops being advice (hook.py rule 1).
  - a KB-only ledger would therefore have no writer at all — which is exactly
    the defect (#8: "the ledger has no writer and would ship as dead schema")
    that blocked this issue for a month, moved one layer up.

Of the three reasons #10 gave for rejecting a local store, one was already
false: `state_dir()` resolves to `~/.local/share/testgraph`, OUTSIDE every
worktree, so `wtclean` was never a risk. The two that stand — invisibility from
the work Mac, and no reviewer/browsable surface — are what `--export-kb` exists
to close, by handing an agent a payload to propose.
"""
import json
import os
import subprocess
import time

LEDGER_NAME = "ledger.jsonl"

# The pre-push hook wrote here before this module existed (issue #49's
# scoreboard). Zero rows on this machine, but reading it costs six lines and
# means no install anywhere silently loses its history to a rename.
LEGACY_NAME = "invocations.jsonl"

SELECTION = "selection"
OUTCOME = "outcome"
VERDICTS = ("pass", "fail", "skip")

# Judged commits — those carrying BOTH a selection and at least one outcome —
# needed before ranking should consult failure history. 20 is not arbitrary: the
# seeded-regression eval (#5) needed ~20 commits before it could say anything
# falsifiable about this selector, and a ranking signal is a weaker instrument
# than that eval, not a stronger one. Below this the ledger reports its counts
# and declines to rank, rather than manufacturing a signal from three rows.
MIN_JUDGED_COMMITS = 20


def state_dir():
    return os.environ.get(
        "TESTGRAPH_STATE_DIR",
        os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "testgraph",
        ),
    )


def path(directory=None):
    return os.path.join(directory or state_dir(), LEDGER_NAME)


def append(row):
    """Append one row. Returns True on success, False on any I/O failure.

    Never raises: every caller is either a git hook that must not fail a push or
    a CLI reporting its own result. A ledger that can break the thing it is
    observing is worse than no ledger."""
    try:
        directory = state_dir()
        os.makedirs(directory, exist_ok=True)
        with open(path(directory), "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def read(repo=None, directory=None):
    """Every well-formed row, oldest first, optionally filtered to one repo.

    A malformed line is SKIPPED, not fatal. The file is appended to by a hook
    that can be killed mid-write (a push interrupted at the wrong microsecond
    leaves a partial line), and one torn line must not blind every reader that
    follows it."""
    directory = directory or state_dir()
    rows = []
    for name, default_kind in ((LEGACY_NAME, SELECTION), (LEDGER_NAME, None)):
        try:
            with open(os.path.join(directory, name)) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if default_kind and "kind" not in row:
                row["kind"] = default_kind
            if repo is None or row.get("repo") == repo:
                rows.append(row)
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def resolve_commit(repo, rev="HEAD"):
    """Full sha for `rev` in `repo`, or `rev` unchanged if git cannot say.

    Selection and outcome rows only join if both name the same commit the same
    way. The hook is handed full shas by git on stdin; a human running `record`
    types "HEAD". Normalising both here is what makes the join work at all."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", rev],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return rev
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else rev


def selection_row(record):
    """Shape a `hook.run` record as a selection row."""
    row = dict(record)
    row["kind"] = SELECTION
    row.setdefault("commit", row.get("head"))
    return row


def outcome_row(repo, commit, journey, verdict, note=None, ts=None):
    row = {
        "kind": OUTCOME,
        "ts": int(ts if ts is not None else time.time()),
        "repo": repo,
        "commit": commit,
        "journey": journey,
        "verdict": verdict,
    }
    if note:
        row["note"] = note
    return row


def summarize(repo, rows=None, directory=None):
    """Per-journey run history plus the recall the ledger has actually observed.

    Three buckets for a failure, and keeping them apart is the whole point:

      caught   — the selection for that commit named this journey. The selector
                 earned its keep.
      missed   — a selection EXISTS for that commit and did not name it. This is
                 a real silent under-selection.
      unjudged — no selection row for that commit at all. Says nothing about the
                 selector; the tool was never asked.

    Collapsing `unjudged` into `missed` would score every failure recorded
    before the hook was installed as a recall miss, which would make the number
    look terrible for a reason that has nothing to do with the selector. Keeping
    them apart is why `observed_recall` is None until something is judged."""
    rows = rows if rows is not None else read(repo, directory=directory)
    rows = [r for r in rows if r.get("repo") == repo]

    named = {}
    for r in rows:
        if r.get("kind") != SELECTION:
            continue
        commit = r.get("commit") or r.get("head")
        if not commit:
            continue
        ids = r.get("journey_ids") or []
        # Repeated pushes of the same commit: union, because a journey named by
        # any selection for that commit was named.
        named.setdefault(commit, set()).update(ids)

    journeys = {}
    judged_commits = set()
    caught_total = missed_total = 0
    for r in rows:
        if r.get("kind") != OUTCOME:
            continue
        jid = r.get("journey")
        commit = r.get("commit")
        verdict = r.get("verdict")
        if not jid:
            continue
        j = journeys.setdefault(
            jid,
            {
                "runs": 0,
                "passes": 0,
                "failures": 0,
                "caught": 0,
                "missed": 0,
                "unjudged": 0,
                "last_ts": None,
            },
        )
        j["runs"] += 1
        if r.get("ts") is not None:
            j["last_ts"] = max(j["last_ts"] or 0, r["ts"])
        if verdict == "pass":
            j["passes"] += 1
        elif verdict == "fail":
            j["failures"] += 1
            if commit in named:
                judged_commits.add(commit)
                if jid in named[commit]:
                    j["caught"] += 1
                    caught_total += 1
                else:
                    j["missed"] += 1
                    missed_total += 1
            else:
                j["unjudged"] += 1
        if commit in named:
            judged_commits.add(commit)

    judged = caught_total + missed_total
    return {
        "repo": repo,
        "selections": sum(1 for r in rows if r.get("kind") == SELECTION),
        "outcomes": sum(1 for r in rows if r.get("kind") == OUTCOME),
        "judged_commits": len(judged_commits),
        "min_judged_commits": MIN_JUDGED_COMMITS,
        "ready_for_ranking": len(judged_commits) >= MIN_JUDGED_COMMITS,
        "caught": caught_total,
        "missed": missed_total,
        "observed_recall": (caught_total / judged) if judged else None,
        "journeys": journeys,
    }
