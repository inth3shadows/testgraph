"""`testgraph record` — write what a journey run FOUND, and read back what the
ledger has learned.

The selector has had a caller since #51 (the pre-push hook), so the ledger gets
`selection` rows for free. This is the other half: the writer for `outcome`
rows, which nothing else can produce, because running a journey needs an
environment and a human or agent that knows whether it worked. #8 answered who
that is — /autorun runs the journeys, Proxmox snapshots reset them — and this is
the interface it writes through.

    python3 -m testgraph.record --repo R --journey J3 --outcome fail --note "..."
    python3 -m testgraph.record --repo R --summary
    python3 -m testgraph.record --repo R --summary --export-kb

Deliberately NOT here: any change to how `select` ranks. Issue #10 asks for
staleness and failure history to feed the next ranking, and it should — but the
ledger currently holds ZERO rows (the hook has not fired once since it merged).
Ranking on an empty history is inventing a signal, so `--summary` reports how
far it is from `ledger.MIN_JUDGED_COMMITS` and ranking stays untouched until
that threshold is real.
"""
import argparse
import json
import os
import sys
import time

from . import ledger
from . import registry as reg


def known_journeys(repo, registry_path=None):
    """{journey_id: name} for a repo, or None if it has no registry.

    An unknown id is rejected rather than stored. A typo'd `--journey J33` in a
    write-only log is invisible forever, and it does not just lose one row: it
    silently deflates that journey's failure count, which is the number this
    ledger exists to produce."""
    registry_path = registry_path or reg.resolve_for_repo(repo)
    if registry_path is None:
        return None
    try:
        registry = reg.load(registry_path)
    except (OSError, ValueError):
        return None
    journeys = registry.get("journeys") or {}
    if not isinstance(journeys, dict):
        return None
    return {jid: (j or {}).get("name", "") for jid, j in journeys.items()}


def add_outcome(repo, journey, verdict, commit=None, note=None, registry_path=None):
    """Append one outcome row. Returns (row, error) — error is a string or None."""
    if verdict not in ledger.VERDICTS:
        return None, f"unknown outcome {verdict!r} (expected one of {', '.join(ledger.VERDICTS)})"
    if not os.path.isdir(repo):
        return None, f"no such repo: {repo}"

    name = reg.repo_name(repo)
    known = known_journeys(repo, registry_path)
    if known is None:
        return None, (
            f"no journey registry for {name} — draft one with "
            f"`python3 -m testgraph.propose --repo {repo}`"
        )
    if journey not in known:
        return None, (
            f"unknown journey {journey!r} for {name}; "
            f"registry has {', '.join(sorted(known))}"
        )

    row = ledger.outcome_row(
        name, ledger.resolve_commit(repo, commit or "HEAD"), journey, verdict, note
    )
    if not ledger.append(row):
        return None, f"could not write {ledger.path()}"
    return row, None


def render_summary(summary, known=None):
    repo = summary["repo"]
    lines = [f"testgraph ledger[{repo}]: {ledger.path()}"]
    if not summary["selections"] and not summary["outcomes"]:
        lines.append("  no rows yet — nothing has been selected or recorded for this repo")
        lines.append(
            f"  ranking will not consult failure history until "
            f"{summary['min_judged_commits']} commits carry both a selection and an outcome"
        )
        return "\n".join(lines)

    lines.append(
        f"  {summary['selections']} selection(s), {summary['outcomes']} outcome(s), "
        f"{summary['judged_commits']} judged commit(s)"
    )
    if summary["observed_recall"] is None:
        lines.append(
            "  observed recall: n/a — no failure has yet been recorded on a "
            "commit that testgraph also answered for"
        )
    else:
        lines.append(
            f"  observed recall: {summary['observed_recall']:.2f} "
            f"({summary['caught']} caught / {summary['caught'] + summary['missed']} judged failures)"
        )
    if summary["missed"]:
        lines.append(
            f"  ! {summary['missed']} SILENT MISS(ES) — a journey failed on a commit "
            f"whose selection did not name it"
        )

    if summary["journeys"]:
        lines.append("  per journey:")
        for jid in sorted(summary["journeys"], key=reg.journey_sort_key):
            j = summary["journeys"][jid]
            label = (known or {}).get(jid, "")
            last = (
                time.strftime("%Y-%m-%d", time.localtime(j["last_ts"]))
                if j["last_ts"]
                else "never"
            )
            flag = "  ! MISSED" if j["missed"] else ""
            lines.append(
                f"    {jid}  {label:<20.20} runs {j['runs']:>3}  fail {j['failures']:>3}  "
                f"caught {j['caught']:>3}  missed {j['missed']:>3}  "
                f"unjudged {j['unjudged']:>3}  last {last}{flag}"
            )

    if not summary["ready_for_ranking"]:
        lines.append(
            f"  ranking does NOT consult this yet: {summary['judged_commits']}/"
            f"{summary['min_judged_commits']} judged commits"
        )
    return "\n".join(lines)


def kb_payload(summary):
    """A proposal an AGENT pushes into the KB — this CLI deliberately does not.

    #10 wants the ledger visible from the work Mac and reviewable, which means
    the KB. It is reachable only through the `kb.*` MCP, which only an agent can
    call, so the honest split is: this emits the content, an agent proposes it.

    The target table is NOT chosen here on purpose. KB conventions require
    `kb.read.search` before proposing, and `kb.propose.extend` on an existing
    node in preference to a new table — a decision that needs the KB in front of
    it, not a hardcoded guess in a CLI that has never read it."""
    j = summary["journeys"]
    recall = summary["observed_recall"]
    content = (
        f"testgraph journey ledger for {summary['repo']}: "
        f"{summary['selections']} selections, {summary['outcomes']} journey outcomes, "
        f"{summary['judged_commits']} commits carrying both. "
        + (
            f"Observed recall {recall:.2f} over {summary['caught'] + summary['missed']} "
            f"judged failures ({summary['missed']} silent miss(es))."
            if recall is not None
            else "No judged failures yet — observed recall is not yet measurable."
        )
    )
    return {
        "proposal": {
            "content": content,
            "evidence": json.dumps(
                {
                    "source": "testgraph.record --summary",
                    "repo": summary["repo"],
                    "selections": summary["selections"],
                    "outcomes": summary["outcomes"],
                    "judged_commits": summary["judged_commits"],
                    "caught": summary["caught"],
                    "missed": summary["missed"],
                    "observed_recall": recall,
                    "per_journey": j,
                },
                sort_keys=True,
            ),
            "applies_to": "personal",
        },
        "next": (
            "run kb.read.search('testgraph journey ledger') first; prefer "
            "kb.propose.extend on an existing node over creating a table (issue #10)"
        ),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="testgraph.record",
        description="record what a journey run found; read back what the ledger knows",
    )
    ap.add_argument("--repo", required=True, help="path to the target repo")
    ap.add_argument("--journey", help="journey id, e.g. J3")
    ap.add_argument(
        "--outcome", choices=ledger.VERDICTS, help="what the journey run found"
    )
    ap.add_argument("--commit", default=None, help="commit the run exercised (default: HEAD)")
    ap.add_argument("--note", default=None, help="root-cause note, free text")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--summary", action="store_true", help="print the ledger summary")
    ap.add_argument(
        "--export-kb",
        action="store_true",
        help="emit the summary as a KB proposal payload for an agent to push",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    wrote = None
    if args.journey or args.outcome:
        if not (args.journey and args.outcome):
            ap.error("--journey and --outcome must be given together")
        wrote, err = add_outcome(
            args.repo,
            args.journey,
            args.outcome,
            commit=args.commit,
            note=args.note,
            registry_path=args.registry,
        )
        if err:
            print(f"testgraph record: {err}", file=sys.stderr)
            return 2
    elif not (args.summary or args.export_kb):
        ap.error("nothing to do: pass --journey/--outcome to write, or --summary to read")

    summary = ledger.summarize(reg.repo_name(args.repo))

    if args.export_kb:
        print(json.dumps(kb_payload(summary), indent=2, sort_keys=True))
        return 0
    if args.json:
        print(json.dumps({"wrote": wrote, "summary": summary}, indent=2, sort_keys=True))
        return 0

    if wrote:
        print(
            f"recorded {wrote['journey']} {wrote['verdict']} at "
            f"{wrote['commit'][:9]} in {wrote['repo']}"
        )
    if args.summary:
        print(render_summary(summary, known_journeys(args.repo, args.registry)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
