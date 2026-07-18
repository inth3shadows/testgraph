# Usage Guide: testgraph

## What This Does

testgraph answers one question about a code change: "which user-facing flows
could this have broken, and in what order should they be tested?" It reads the
change, consults a map of how the code connects to your app's journeys, and
hands back a short, ranked list instead of "re-run everything."

It is built to be cautious: it would rather list a flow that turns out fine than
miss one that actually broke. So on a big shared change (like an app-wide
setting) it may list every flow — that is on purpose, not a mistake.

## How to Use It

Run it after making changes, pointing it at the project:

```
python3 -m testgraph.select --repo <path-to-project>
```

It prints the flows worth testing, most important first, for example:

```
journeys to test (3), ranked:
  [ 12] J8  auto-scheduler  (2 entry)
  [  4] J4  reschedule      (1 entry)
  [  0] J7  gcal sync       (1 entry)
```

Test those flows in that order. If it prints "journeys to test: NONE", the
change didn't touch any tracked user flow (e.g. a docs, test, or health-check
edit).

## What to Do When Something Breaks

- **"STATUS: BLOCKED — index not trustworthy"** — the underlying code map is
  stale or damaged. Rebuild it with `codegraph index <path-to-project>` (use
  `index`, not `sync` — `sync` does not fix this), then run testgraph again.
- **"spot-check symbol ... missing" or "... likely corrupt"** — same fix: a full
  `codegraph index` rebuild.
- **A "WARN" about files newer than the index** — the map is slightly behind the
  code. Results are still safe to act on; refresh with `codegraph sync` when
  convenient.
- **The list looks too long** — expected on shared changes (config, shared data
  models). testgraph errs toward listing more rather than missing one.
- **The list looks too short / missed a flow you expected** — the flow may be
  missing an entry point in the registry (`journeys/honeyslate.json`). Add it and
  re-run.

## FAQ

**Does it run the tests for me?** No — it decides *what* to test. Running is done
by you, CI, or a test-driver like Playwright.

**Which projects does it support today?** honeyslate, Python code only.

**Can I trust "NONE"?** Yes, as long as the guard didn't block — that means no
tracked user flow was affected.

For anything not covered here, contact Eric (eric.minish@gmail.com).
