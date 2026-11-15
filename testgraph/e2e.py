"""Run the Playwright tests declared for the journeys a diff endangers (#122).

The e2e counterpart of `verify.run`. A registry opts in with

    "e2e": {"runner": "playwright", "cwd": "frontend",
            "config": "playwright.real.config.js"}

and a test declares its journey with Playwright's tag, in the project's one
convention (`results.DEFAULT_TAG_CONVENTIONS["playwright"]`):

    test("a submitter adds a task", { tag: "@tg:J1" }, async ({ page }) => ...)

What this does NOT yet know is whether a passing test actually reached the
journey's entry symbols. Nothing traces the backend during a browser run, so a
test that mocks the app's own API passes just the same. Every verdict here is
therefore DECLARED, not trace-validated, and says so: provenance
`e2e-declared`, and the rendered summary names it. Backend tracing is #157.

Verdicts, per selected journey, from Playwright's JSON report:
  - any tagged test unexpected (failed, timed out, crashed)  -> fail  (exit 1)
  - any tagged test flaky (passed only on a retry)            -> incomplete
  - a tagged test passed                                      -> pass
  - every tagged test skipped                                 -> incomplete
  - no tagged test at all                                     -> uncovered (exit 3)
An untagged test that fails (the login/seed setup project) is a broken
environment, not a verdict about any journey: the run is refused (exit 2), as is
a report with global errors or no report at all.
"""
import json
import os
import re
import subprocess
import tempfile

from . import ledger
from . import results as res

PROVENANCE = "e2e-declared"
RUNNERS = ("playwright",)


def tag(journey):
    return res.format_tag(res.DEFAULT_TAG_CONVENTIONS["playwright"], journey)


def grep_expression(journeys):
    """`(@tg:J1|@tg:J2)(\\s|$)` — Playwright `--grep` for exactly these journeys.

    The trailing boundary is load-bearing: a bare `@tg:J1` also matches
    `@tg:J10`. Playwright matches the regex against the title plus its tags."""
    alts = "|".join(re.escape(tag(j)) for j in journeys)
    return f"({alts})(\\s|$)"


def grep_argument(journeys):
    """`grep_expression` as a `/.../` literal. Playwright compiles a bare
    `--grep` with flags "gi" (forceRegExp, lib/util.js), so `@tg:j1` and
    `@TG:J1` would run while the report's tags cannot be read as J1, and a
    failure could vanish (review P5). A literal with no flags is case-sensitive."""
    return f"/{grep_expression(journeys)}/"


#: Flags whose value verify sets itself: a second one would replace the journey
#: filter, the JSON reporter or the config (review P4).
OWNED_FLAGS = ("--grep", "-g", "--grep-invert", "--reporter", "--config", "-c")


def owned_flag(args):
    """The first arg that would override what verify sets, or None. Short flags
    can be clustered or carry their value (`-xg smoke`, `-gsmoke`)."""
    for a in args:
        if a.split("=", 1)[0] in OWNED_FLAGS:
            return a
        if not a.startswith("--") and re.match(r"-[A-Za-z]*[gc]", a):
            return a
    return None


def check_config(repo, e2e):
    """(cwd, config, error) for the registry's `e2e` block. Every bad shape is a
    refusal: a traceback would exit 1, the "tests failed" code (review P1), and
    a cwd or config outside the repo would run someone else's tests (P3)."""
    if not isinstance(e2e, dict):
        return None, None, (f"registry e2e must be an object like "
                            f'{{"runner": "playwright", "cwd": "frontend"}}, got {e2e!r}')
    if e2e.get("runner") not in RUNNERS:
        return None, None, (f"registry e2e.runner must be one of {RUNNERS}, "
                            f"got {e2e.get('runner')!r}")
    cwd = e2e.get("cwd")
    if not isinstance(cwd, str) or not cwd or os.path.isabs(cwd):
        return None, None, (f"registry e2e.cwd must be a path relative to the repo "
                            f'("." for its root), got {cwd!r}')
    root = os.path.realpath(repo)

    def inside(path):
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    full = os.path.normpath(os.path.join(repo, cwd))
    if not inside(full):
        return None, None, f"registry e2e.cwd {cwd!r} resolves outside the repo"
    if not os.path.isdir(full):
        return None, None, f"registry e2e.cwd does not exist: {full}"
    config = e2e.get("config")
    if config is not None:
        if (not isinstance(config, str) or not config or config.startswith("-")
                or os.path.isabs(config)):
            return None, None, (f"registry e2e.config must be a path relative to "
                                f"e2e.cwd, got {config!r}")
        if not inside(os.path.join(full, config)):
            return None, None, f"registry e2e.config {config!r} resolves outside the repo"
    return full, config, None


def _message(err):
    return str(err.get("message", err) if isinstance(err, dict) else err)


def _specs(suite):
    for spec in suite.get("specs", []):
        yield spec
    for child in suite.get("suites", []):
        yield from _specs(child)


def parse_report(report, journeys):
    """Playwright's JSON report -> (per_journey, untagged_failures, unknown_tags).

    `per_journey`: {jid: [(title, status), ...]} for the selected journeys,
    where status is Playwright's test status (`expected`, `unexpected`,
    `flaky`, `skipped`) refined so an `expected` whose last attempt did not
    pass (a `test.fail()` expectation) is not read as a pass."""
    prefix = tag("")[1:]  # "tg:" — JSON tags carry no leading '@'
    wanted = set(journeys)
    per_journey = {j: [] for j in journeys}
    untagged_failures, unknown = [], set()
    for suite in report.get("suites", []):
        for spec in _specs(suite):
            ids = [t[len(prefix):] for t in spec.get("tags", []) if t.startswith(prefix)]
            for test in spec.get("tests", []):
                status = test.get("status")
                results = test.get("results") or []
                if status == "expected" and (not results
                                             or results[-1].get("status") != "passed"):
                    status = "expected-not-passed"
                if not wanted.intersection(ids):
                    # Untagged, or tagged only for journeys not selected (a
                    # setup project, a mis-cased tag): it is not a verdict
                    # about any selected journey, and a failure here means the
                    # environment is broken (review P5). Never dropped.
                    if status in ("unexpected", "flaky"):
                        untagged_failures.append(spec.get("title", "?"))
                    unknown.update(j for j in ids if j not in wanted)
                    continue
                for jid in ids:
                    if jid in wanted:
                        per_journey[jid].append((spec.get("title", "?"), status))
                    else:
                        unknown.add(jid)
    return per_journey, untagged_failures, sorted(unknown)


def judge(per_journey):
    """(credited, incomplete, uncovered) in verify's summary vocabulary."""
    credited, incomplete, uncovered = {}, [], []
    for jid, tests in per_journey.items():
        statuses = {s for _title, s in tests}
        if not tests:
            uncovered.append(jid)
        elif "unexpected" in statuses:
            credited[jid] = "fail"
        elif "flaky" in statuses:
            incomplete.append((jid, "flaky"))
        elif "expected" in statuses:
            credited[jid] = "pass"
        elif statuses == {"expected-not-passed"} or (
                "expected-not-passed" in statuses and "skipped" not in statuses):
            # test.fail(): it ran, and failed as declared. Not a pass, and not
            # a skip either (review P7).
            incomplete.append((jid, "expected-fail"))
        else:
            credited[jid] = "skip"
            incomplete.append((jid, "skip"))
    return credited, incomplete, uncovered


def run(repo, journeys, config, extra_args=(), _runner=None):
    """Run Playwright for `journeys`. Returns (fields, error) — `fields` are the
    summary keys verify merges in; `error` is a refusal message."""
    cwd, cfg, err = check_config(repo, config)
    if err:
        return None, err
    own = owned_flag(extra_args)
    if own:
        return None, owned_refusal(own)
    expr = grep_argument(journeys)
    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "report.json")
        # --pass-with-no-tests: a selection nobody tagged is `uncovered`, not
        # Playwright's "No tests found" run error (review P2).
        cmd = ["npx", "--no-install", "playwright", "test", "--grep", expr,
               "--reporter=json", "--pass-with-no-tests"]
        if cfg:
            cmd[4:4] = ["-c", cfg]
        cmd += list(extra_args)
        # _FILE wins over _NAME in Playwright, so an inherited one would send
        # the report elsewhere (and overwrite the user's file): set both (P4).
        env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=report_path,
                   PLAYWRIGHT_JSON_OUTPUT_FILE=report_path)
        runner = _runner or (lambda c, d, e: subprocess.run(
            c, cwd=d, env=e, capture_output=True, text=True))
        try:
            proc = runner(cmd, cwd, env)
        except OSError as exc:
            return None, f"could not start Playwright ({exc}) — is Node installed?"
        tail = (getattr(proc, "stderr", "") or "").strip()[-400:]
        if not os.path.isfile(report_path):
            return None, (f"Playwright exited {getattr(proc, 'returncode', '?')} "
                          f"and wrote no JSON report — no verdict was recorded"
                          + (f"\n  stderr (tail): {tail}" if tail else ""))
        try:
            with open(report_path) as fh:
                report = json.load(fh)
        except (OSError, ValueError) as exc:
            return None, f"the Playwright report is unreadable ({exc}) — no verdict was recorded"
    if not isinstance(report, dict):
        return None, "the Playwright report is not a JSON object — no verdict was recorded"
    errors = report.get("errors") or []
    # maxFailures / -x stop the run with a global error, but a tagged test
    # that already failed is red whatever came after it (review P6). Any
    # other run error, or a stop with no tagged failure, is a refusal.
    stopped = bool(errors) and all("maximum allowed failures" in _message(e)
                                   for e in errors)
    if errors and not stopped:
        return None, f"Playwright reported a run error: {_message(errors[0])[:300]}"
    per_journey, untagged_failures, unknown = parse_report(report, journeys)
    if untagged_failures:
        return None, (f"an untagged Playwright test failed, or one tagged only for "
                      f"journeys not selected ({', '.join(untagged_failures[:3])}) — "
                      f"setup or environment is broken, so no journey verdict was "
                      f"recorded")
    credited, incomplete, uncovered = judge(per_journey)
    if stopped and "fail" not in credited.values():
        return None, f"Playwright reported a run error: {_message(errors[0])[:300]}"
    return {
        "runner": "playwright",
        "marker_expression": expr,
        "collected": sum(len(t) for t in per_journey.values()),
        "credited": credited,
        "per_journey": {j: [title for title, _s in t] for j, t in per_journey.items() if t},
        "uncovered": uncovered,
        "incomplete": incomplete,
        "unknown_tags": unknown,
        "runner_exit": getattr(proc, "returncode", None),
    }, None


def owned_refusal(flag):
    return (f"verify --runner playwright owns --grep, --reporter and --config "
            f"(it selects the journeys' @tg: tags and reads the JSON report); "
            f"drop {flag!r} — narrow the run with a spec path or --project instead")


def write_rows(repo_name, sha, fields, append=None):
    append = append or ledger.append
    written = 0
    for jid, verdict in sorted(fields["credited"].items()):
        if verdict not in ("pass", "fail"):
            continue
        row = ledger.outcome_row(
            repo_name, sha, jid, verdict,
            note=(f"{len(fields['per_journey'].get(jid, ()))} tagged Playwright "
                  f"test(s) — declared, not trace-validated"),
            edge_provenance=PROVENANCE,
        )
        if append(row):
            written += 1
    return written
