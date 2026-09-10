"""Phase 3 of the verification-direction plan (Phase 5 closes issue #12/#66):
the normalized result record, the tag convention, and the `runners:` spec.

Deliberately NOT here: any actual test execution, subprocess invocation, or
JUnit-XML parsing. Those are Phase 4 (a pytest adapter over `tgtrace.py`) and
whatever adapter each later runner gets. This module is the durable half — the
schema and naming convention third parties integrate against — kept separate
from the runner-specific mechanics so the schema can outlive any one adapter
(see `~/.claude/plans/testgraph-verification-direction.md` D3).

Design decision, logged inline per the usual convention: `runners:` config is
accepted here as an already-parsed `dict`, never as YAML text. `pyproject.toml`
asserts `dependencies = []` (stdlib-only) — adding PyYAML just to read one
config block would break that claim for every consumer of this package,
including ones that never touch this feature. A `.yaml` file on disk is still
fine; whoever loads it (a CI script, an agent) parses it with whatever YAML
library is already in their environment and hands `parse_runners_spec` the
resulting dict. JSON works too, via the stdlib `json` module, with no
conversion needed at all.
"""
import re

STATUSES = ("pass", "fail", "skip", "error")

# Maps a raw per-test-run STATUS onto the ledger's 3-way VERDICT
# (testgraph.ledger.VERDICTS). The ledger predates this module and only knows
# pass/fail/skip; a runner's `error` (uncaught exception, setup failure) is a
# real, distinct signal worth keeping on the raw record, but it still reads as
# a failure once it reaches the ledger — a `describe.skipIf`-shaped runner
# that "erroed" and one that plainly failed a broken app the same way.
TO_LEDGER_VERDICT = {"pass": "pass", "fail": "fail", "error": "fail", "skip": "skip"}


def run_result(runner, test_id, status, journeys=(), duration_s=None, message=None,
               artifacts=None, ts=None):
    """One normalized per-test-run record — the shape every adapter produces,
    whatever the runner. `journeys` is a tuple/list because one test can carry
    more than one journey tag (a broad integration test legitimately covers
    two journeys); it is empty, not omitted, when a test's tags name none —
    that distinction is what lets a later reduction step tell "ran, untagged"
    apart from "ran, tagged, and this is what happened."

    Raises ValueError on an unknown `status` — a typo'd status silently
    dropped from every later count is worse than a loud failure here, the same
    reasoning as `ledger.append`'s VERDICTS check in `record.add_outcome`."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} (expected one of {', '.join(STATUSES)})")
    return {
        "runner": runner,
        "test_id": test_id,
        "status": status,
        "journeys": list(journeys),
        "duration_s": duration_s,
        "message": message,
        "artifacts": dict(artifacts) if artifacts else {},
        "ts": ts,
    }


# Reference defaults a generated runners.yaml can start from — not enforced;
# `parse_runners_spec` accepts any per-runner `tag` template a user declares.
# One `{journey}` placeholder each, e.g. "J4" -> "tg_J4" / "@tg:J4" / "TestJ4_".
DEFAULT_TAG_CONVENTIONS = {
    "pytest": "tg_{journey}",
    "playwright": "@tg:{journey}",
    "cypress": "@tg:{journey}",
    "go": "TestJ{journey}_",
    "junit": "tg:{journey}",
}


def format_tag(template, journey):
    """Expand a tag template for one journey id. The one primitive every
    adapter needs, regardless of runner-specific join syntax (pytest's marker
    `or`, Playwright's grep `|`, ...) — which is why joining several tags into
    one selection expression is left to each adapter (Phase 4+), not decided
    here for all of them at once."""
    return template.format(journey=journey)


_JOURNEY_GROUP = r"(?P<journey>[A-Za-z0-9_]+)"


def _template_regex(template):
    """Turn a one-placeholder tag template into a compiled matcher. Requires
    exactly one `{journey}` — a template with zero or several has no single
    well-defined inverse, and failing loudly here beats guessing which
    occurrence was meant."""
    parts = template.split("{journey}")
    if len(parts) != 2:
        raise ValueError(
            f"tag template {template!r} must contain exactly one {{journey}} placeholder"
        )
    return re.compile(re.escape(parts[0]) + _JOURNEY_GROUP + re.escape(parts[1]))


def parse_journey_from_tag(template, text):
    """The inverse of `format_tag`: given the template and an observed string
    (a pytest marker name, a parsed JUnit `<tag>` value, ...), recover the
    journey id, or None if `text` does not match. Used to join a runner's
    native test identity back to a journey after the run, which is the whole
    point of tagging tests in the first place."""
    match = _template_regex(template).fullmatch(text)
    return match.group("journey") if match else None


RUNNER_SPEC_REQUIRED = ("select", "tag", "results")
RUNNER_SPEC_OPTIONAL = ("artifacts", "adapter")


def parse_runners_spec(data):
    """Validate and normalize a `runners:` mapping (already-parsed dict — see
    the module docstring on why this never touches YAML itself).

    Returns (parsed, errors). `parsed` holds only the runners that validated
    cleanly; `errors` names every problem, prefixed with the runner name, so
    one malformed entry does not blind the caller to the rest — the same
    tolerance `ledger.read()` gives a torn line, applied to config instead of
    a log."""
    if not isinstance(data, dict):
        return {}, [f"runners spec must be a mapping, got {type(data).__name__}"]

    parsed = {}
    errors = []
    for name, spec in data.items():
        if not isinstance(spec, dict):
            errors.append(f"{name}: spec must be a mapping, got {type(spec).__name__}")
            continue
        missing = [k for k in RUNNER_SPEC_REQUIRED if not spec.get(k)]
        if missing:
            errors.append(f"{name}: missing required field(s): {', '.join(missing)}")
            continue
        bad_types = [
            k for k in (*RUNNER_SPEC_REQUIRED, *RUNNER_SPEC_OPTIONAL)
            if k in spec and not isinstance(spec[k], str)
        ]
        if bad_types:
            errors.append(f"{name}: field(s) must be strings: {', '.join(bad_types)}")
            continue
        try:
            _template_regex(spec["tag"])
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
        entry = {k: spec[k] for k in RUNNER_SPEC_REQUIRED}
        for k in RUNNER_SPEC_OPTIONAL:
            if k in spec:
                entry[k] = spec[k]
        parsed[name] = entry
    return parsed, errors
