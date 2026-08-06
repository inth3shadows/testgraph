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
    rows.sort(key=_ts)
    return rows


def _ts(row):
    """Sortable timestamp. A row whose `ts` is not a number sorts first rather
    than raising — `read()` promises a malformed row is skipped, not fatal, and
    a bare `r.get("ts") or 0` breaks that promise on a row that is valid JSON,
    is a dict, and passes every other guard. The KB export loop invites an agent
    to write into this file, so a hand-edited `ts` is a realistic input."""
    value = row.get("ts")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def is_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdef" for c in value)
    )


def resolve_commit(repo, rev="HEAD"):
    """Full commit sha for `rev` in `repo`, or **None** if git cannot say.

    Selection and outcome rows only join if both name the same commit the same
    way. The hook is handed full shas by git on stdin; a human running `record`
    types "HEAD". Normalising both here is what makes the join work at all.

    Returning None rather than the rev verbatim is the whole point. A failed
    resolution used to store the literal string — so `record --repo
    ~/personal_projects/honeyslate` (the bare-worktree *parent*, a directory
    whose repo_name still resolves, so every other check passes) filed every
    outcome under the key `"HEAD"`, and two unrelated commits' failures then
    joined to each other: one fabricated catch and one fabricated miss from a
    single unjoinable key.

    `^{commit}` because a bare `rev-parse v1.0` on an ANNOTATED tag returns the
    tag OBJECT's oid, which no selection row can ever carry — the outcome would
    sit in `unasked` forever with nothing to show why."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", f"{rev}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and is_sha(sha) else None


def commit_of(row):
    """The join key for a row, or None if it has no usable one.

    Legacy `invocations.jsonl` rows predate the `commit` field, but the hook was
    handed full shas by git, so their `head` is a real key — while a manual
    `hook.main --head HEAD` run wrote the literal string. Accepting only a sha
    from the fallback keeps the second kind out of the join."""
    commit = row.get("commit")
    if is_sha(commit):
        return commit
    head = row.get("head")
    return head if is_sha(head) else None


def base_of(row):
    """The push's BASE as a join key, or None. The mirror of `commit_of`.

    `hook.run` records `base` as the caller SPELLED it and the resolved form in
    `base_commit`. The baseline lookup joins against outcome rows keyed on
    resolved shas, so accepting only a sha here is what keeps a symbolic
    spelling from silently matching nothing — `base` is still read as a
    fallback because git hands the hook a real sha on the ordinary path."""
    base = row.get("base_commit")
    if is_sha(base):
        return base
    base = row.get("base")
    return base if is_sha(base) else None


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

    Four buckets for a failure, and keeping them apart is the whole point:

      caught      — the selection ANSWERED for that push and named this journey,
                    and the journey was known-good at the push's base. The
                    selector earned its keep.
      missed      — the selection ANSWERED for that push and did not name it, and
                    the journey was known-good at the push's base. A real silent
                    under-selection.
      unasked     — no selection answered for that commit. The tool was never
                    asked, so this says nothing about it.
      unbaselined — a selection answered, but nothing records the journey passing
                    at that push's BASE, so the breakage may predate the push.

    The green baseline gates `caught` and `missed` IDENTICALLY, and that symmetry
    is load-bearing. Requiring it for `missed` alone — the state this function
    shipped in — made pre-existing breakage able to raise `observed_recall` and
    never able to lower it. A journey that was never green scored `caught` on
    every push whose selection happened to name it, which is a perfect score for
    zero information: the journey was already red, so naming it predicted
    nothing. The same history with a selector that named nothing was excluded
    from scoring entirely as `unbaselined`. Two pushes of an always-red journey
    read 2 caught / 0 missed / recall 1.00 one way and 0/0/None the other.

    That is the same defect as the two the DELTA-vs-STATE fix closed, pointed the
    other way: a row that says nothing about the selector counted as evidence —
    there, against it; here, for it. You only get to CREDIT the selector when
    there was something to regress from, for the same reason you only get to
    blame it then.

    `unbaselined` is the bucket that keeps the number honest, and it is the one
    that was missing. A `selection` row answers "what could base..head break" — a
    DELTA. An `outcome` row asserts "journey J is broken AT this commit" — a
    STATE. Joining them on the head commit alone scores this trace as a miss:

        push A..B breaks J3, and the selection for B correctly NAMES J3
        nobody runs journeys
        push B..C touches only README, so the selection for C names nothing
        the developer runs J3 at HEAD (=C) — exactly what USAGE.md says to do —
        and it fails, so it is recorded at C

    The selector was right both times, and the old join reported a silent miss
    and dropped `observed_recall`. Requiring a recorded `pass` at the push's base
    before blaming the selector is what removes that: you only get to call it a
    miss when you had a green baseline to regress from. The cost is that misses
    are rare unless journeys run on every push — which is the discipline this
    number needs in order to mean anything.

    Only selections whose `status` is OK count as having answered. `hook.run`
    writes rows with no `journey_ids` on four non-answer paths — NO_REGISTRY,
    NO_INDEX, ERROR, and BLOCKED — and treating those as "asked and answered
    nothing" made a tripped integrity guard, or a single JSON typo in an approved
    registry, score every later failure as the selector's fault."""
    rows = rows if rows is not None else read(repo, directory=directory)
    rows = [r for r in rows if r.get("repo") == repo]

    # Selections that ANSWERED, keyed by commit. `base` comes along because the
    # baseline check needs the other end of the range.
    answered = {}
    for r in rows:
        if r.get("kind") != SELECTION or r.get("status") != "OK":
            continue
        commit = commit_of(r)
        if not commit:
            continue
        entry = answered.setdefault(commit, {"named": set(), "bases": set()})
        # Repeated pushes of one commit: union, because a journey named by any
        # selection for that commit was named.
        entry["named"].update(r.get("journey_ids") or [])
        base = base_of(r)
        if base:
            entry["bases"].add(base)

    # One observation per (commit, journey); the last verdict wins. Recording the
    # same failure twice — a re-run to confirm, or /autorun logging each attempt —
    # used to multiply the headline miss count for a single defect, while
    # `judged_commits` stayed deduplicated. The two numbers then disagreed.
    latest = {}
    for r in rows:
        if r.get("kind") != OUTCOME:
            continue
        jid, commit = r.get("journey"), r.get("commit")
        if not jid or not commit:
            continue
        key = (commit, jid)
        if key not in latest or _ts(r) >= _ts(latest[key]):
            latest[key] = r

    passed_at = {
        key for key, r in latest.items() if r.get("verdict") == "pass"
    }

    journeys = {}
    judged_commits = set()
    caught_total = missed_total = 0
    for (commit, jid), r in sorted(latest.items(), key=lambda kv: _ts(kv[1])):
        j = journeys.setdefault(
            jid,
            {
                "runs": 0,
                "passes": 0,
                "failures": 0,
                "caught": 0,
                "missed": 0,
                "unasked": 0,
                "unbaselined": 0,
                "last_ts": None,
            },
        )
        verdict = r.get("verdict")
        j["runs"] += 1
        if isinstance(r.get("ts"), (int, float)):
            j["last_ts"] = max(j["last_ts"] or 0, r["ts"])
        entry = answered.get(commit)
        # `judged_commits` measures how DENSE the history is and gates ranking at
        # MIN_JUDGED_COMMITS. It must count only what the score can actually use,
        # or it diverges from `judged` and the gate opens on evidence that was
        # excluded: 24 always-red pushes (all `unbaselined`) plus ONE real catch
        # read as 25 judged commits and opened ranking on an observed_recall of
        # 1.00 drawn from a single observation. A `skip` is likewise not
        # evidence about anything — including density.
        if verdict == "pass":
            j["passes"] += 1
            if entry:
                judged_commits.add(commit)
            continue
        if verdict != "fail":
            continue
        j["failures"] += 1
        if not entry:
            j["unasked"] += 1
        # The baseline is checked BEFORE caught-vs-missed, not as the fallback
        # after it. As a fallback it gated only `missed`, so an always-red
        # journey could bank a `caught` on every push that named it while the
        # same history with a blind selector was excluded from the score.
        elif not any((base, jid) in passed_at for base in entry["bases"]):
            j["unbaselined"] += 1
        elif jid in entry["named"]:
            j["caught"] += 1
            caught_total += 1
            judged_commits.add(commit)
        else:
            j["missed"] += 1
            missed_total += 1
            judged_commits.add(commit)

    judged = caught_total + missed_total
    return {
        "repo": repo,
        "selections": sum(1 for r in rows if r.get("kind") == SELECTION),
        "outcomes": sum(1 for r in rows if r.get("kind") == OUTCOME),
        "judged_commits": len(judged_commits),
        "min_judged_commits": MIN_JUDGED_COMMITS,
        # Both conditions are separately necessary: enough history, and at least
        # one judged FAILURE in it. Twenty commits of `skip` rows used to flip
        # this flag while carrying zero failure evidence — the exact thing the
        # threshold reasons about.
        "ready_for_ranking": len(judged_commits) >= MIN_JUDGED_COMMITS and judged > 0,
        "caught": caught_total,
        "missed": missed_total,
        "observed_recall": (caught_total / judged) if judged else None,
        "journeys": journeys,
    }
