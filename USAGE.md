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

Test those flows in that order. If it prints "journeys to test: NONE" **and no
"RECALL DEGRADED" line**, the change didn't touch any tracked user flow (e.g. a
docs, test, or health-check edit).

`RECALL DEGRADED` means a changed file could not be located in the code map at
all, so every journey is listed and the honest answer is *unknown*, not *none*.

You do not have to pass `--registry`: testgraph picks the registry whose `target`
matches the repo. If none does, it says so and stops rather than answering from
somebody else's journeys.

### Letting it run itself, on every push

Remembering to run a tool is the part that does not happen. Install the hook once:

```
hooks/install.sh                    # every repo with an approved registry
hooks/install.sh /path/to/repo      # or just one
```

From then on, `git push` prints the journeys that push could have broken, before the
code leaves your machine — roughly 30ms, and it **never blocks the push**. A stale
index, a missing registry, or an outright crash prints one line and lets the push
through; you lose the advice, not the work.

Turn it off for one repo with `git config testgraph.enabled false`; remove it
entirely with `hooks/install.sh --uninstall`. The installer shares the hook file
with other tools rather than overwriting it, so an existing `pre-push` keeps working.

Every run appends a line to `~/.local/share/testgraph/invocations.jsonl`. To check
the tool is actually being used: `wc -l ~/.local/share/testgraph/invocations.jsonl`.

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
- **"UNAPPROVED REGISTRY"** — the registry driving this run was drafted by
  `testgraph.propose` and nobody has reviewed it. Results are usable, but treat a
  short list or a `NONE` as *possibly unregistered* rather than *unaffected*. Clear
  it by reviewing the registry and setting `"approved": true`.

## FAQ

**Does it run the tests for me?** No — it decides *what* to test. Running is done
by you, CI, or a test-driver like Playwright.

**Can I use it on another project?** It needs a journey registry for that project.
`python3 -m testgraph.propose --repo <path>` drafts one by finding your route
handlers, then the `testgraph-propose` skill walks an agent through grouping and
approving it. Until someone approves it, every run prints `UNAPPROVED REGISTRY` —
the answers are usable, but a `NONE` may mean "that flow isn't registered yet"
rather than "nothing broke".

**Which projects does it support today?** honeyslate is the only reviewed one. Backend Python and
frontend `.js/.ts/.jsx/.tsx/.svelte/.vue` changes are both analysed; the journeys
themselves are registered against backend entry points.

**Can I trust "NONE"?** Only when all three hold: the guard didn't block, the diff
you asked about was non-empty, and no `RECALL DEGRADED` line appeared. Those are
the two ways a run can say nothing while looking confident — an empty range, and a
changed file the code map has no symbols for.

For anything not covered here, contact Eric (eric.minish@gmail.com).
