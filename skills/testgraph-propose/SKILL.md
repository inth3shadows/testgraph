---
name: testgraph-propose
description: Draft a journey registry for a repo that has none, so testgraph can answer "what could this change have broken?" there. Runs the deterministic proposer, then does the part it deliberately leaves to you — grouping handlers into user journeys, naming them, and closing the declared blind spots. Use when a repo has no journeys/<target>.json, or when an existing draft is still unapproved.
---

# testgraph-propose

testgraph needs a registry mapping user journeys to their entry symbols. Hand-
authoring one is what limits how many repos it covers. `testgraph.propose` does the
mechanical half; **this skill is the judgment half.** Do not skip it and ship the
draft — a draft is deliberately maximally split and knowingly incomplete.

## 1. Run the proposer

```
python3 -m testgraph.propose --repo <path-to-project>
```

Needs a CodeGraph index (`<repo>/.codegraph/codegraph.db`). If it says there is
none, run `codegraph index <repo>` first and re-run. It writes
`journeys/<target>.draft.json` and prints the handlers it found, the ones it
excluded, and its blind spots.

It reads two languages: Python route decorators, and Next.js conventions in
TypeScript/JavaScript (`route.ts` handler exports, module-level `'use server'`
actions, `page.tsx` default exports). Nothing else — see the blind spots.

**Exit code 1 with zero journeys is a finding, not a failure.** It means the repo
has neither decorator-style Python handlers nor Next.js entry points — its journeys start somewhere
this scan cannot see. Go to step 4 and build the registry from there, or report
that testgraph does not fit this repo.

## 2. Group and name — the part the tool refuses to guess

The draft is **one journey per route handler**, named after the route
(`POST /tasks`). That is on purpose: splitting is safe (more journeys, each
narrower, recall unaffected), merging is not (two flows behind one id hides which
one broke). So the tool never merges, and you do.

Merge handlers into a journey **only when a user would call them one flow** and
you would always test them together. Read the handler bodies — route paths alone
will mislead you.

- `GET /tasks`, `GET /tasks/{id}`, `GET /task-types` → one "browse tasks" journey.
- A `PAGE /staff/[id]` and the `ACTION /staff/[id]` beside it are usually one
  journey: the page renders the form, the action submits it. The draft splits
  them because it will not guess; you have the file open and can tell.
- `POST /tasks` stays alone — creating is not browsing.
- `GET /tasks/{id}/comments` + `POST /tasks/{id}/comments` → one "comments"
  journey, despite sharing a path prefix with browse.

When merging, keep every handler in `entries` — an entry you drop is a symbol
whose change will no longer select that journey. Rewrite `name` as the user-facing
flow ("submit task"), not the HTTP verb. Keep ids stable and short (`J1`, `J2`);
the exported map and every past verification quote them.

## 3. Do not delete the `route` field

Each drafted journey carries `route`. It is the only record of which HTTP path an
entry came from, and it is what makes the next review of this registry possible.
Keep it on merged journeys as a list.

## 4. Close the blind spots — this is where under-selection hides

The draft prints what the scan structurally cannot find. Each one is a place a
real journey can exist with no entry in the registry, which makes testgraph answer
`NONE` for changes that do break it. Work the list:

| Blind spot | How to find them |
|---|---|
| Background jobs / schedulers | Search for scheduler setup (`AsyncIOScheduler`, `add_job`, `celery`, `cron`) and register the job function itself. honeyslate's J8 is exactly this — `scheduler.sweep` and `scheduler.start`, no decorator on either. |
| CLI / management commands | `argparse`/`click`/`typer` entry points, `[project.scripts]` in `pyproject.toml`. |
| Callback-registered consumers | Queue and webhook handlers passed as arguments rather than decorated. |
| Middleware / lifespan hooks | `add_middleware`, `@asynccontextmanager` lifespan, startup handlers. |
| TypeScript scanned "for Next.js shapes ONLY" | Express, Fastify, Hono, tRPC, `middleware.ts`, `generateStaticParams`, and pages-router `pages/api/*` are all invisible. Grep for `app.get(`/`router.post(`/`createTRPCRouter` and register what you find. |
| Client-side navigation | A journey reachable only through in-browser routing has no server entry point. Anchor it on the server action or route handler it eventually calls, or accept that it is uncovered and say so. |
| Product files "no scanner here reads" | `.svelte`, `.vue` and anything else. Backend entries still cover such a change *indirectly* through the graph, so this is usually acceptable — but say so rather than assuming it. |
| Files that do not parse | Fix the syntax error and re-run, or the whole module is missing from the draft. |

Add anything you find as an ordinary entry: `{"name": "<symbol>", "file": "<repo-relative path>"}`.

## 5. Verify before approving

Both of these must pass, against the draft, before you set the flag:

```
python3 -m testgraph.select --repo <path> --registry journeys/<target>.draft.json
```

- **No `journeys with no resolvable entry symbol` block.** An entry that does not
  resolve takes down the entire registry, not just its own journey.
- **No `journey ... no definition` drift warning.** That means an entry you added
  by hand does not exist in the source at that path.
- Sanity-check the `spot_checks` block. Floors are set at 80% of the observed
  inbound-edge count, which is a starting point, not a measurement.

  Pins are scored `fan_in / (1 + caller-file churn)` — quiet *and* load-bearing.
  The floor breaks when call sites are deleted, so a volatile symbol is a poor
  pin; but a *quiet* one with tiny fan-in is worse, because its tolerance band is
  under one edge. Candidates whose band is under 2 edges are dropped outright. `spot_check_basis` tells you whether churn data was available; if it
  says `fan-in only`, the repo has no usable git history and the pins are
  unranked for stability, so check them yourself.

  `spot_check_candidates` lists the runners-up with their fan-in and churn. To
  swap a pin, take one from there rather than inventing a symbol — the floor has
  already been derived against the same edge kinds the guard measures.

## 6. Approve

Set `"approved": true`, drop the `blind_spots` you resolved (keep the ones you
consciously accepted), rewrite `note` to say who reviewed it and what is knowingly
uncovered, and rename the file to `journeys/<target>.json`.

Until that flag is set, every `select` run and every exported map carries an
`UNAPPROVED REGISTRY` warning — which is correct, and is why shipping the draft
unreviewed is not a shortcut.

## What approval means

You are asserting one thing: **a `NONE` answer from this registry means "no
registered journey is affected", and you have checked that the registry covers the
journeys this app actually has.** You are not asserting the grouping is optimal.
If you cannot make that claim, leave it unapproved and say which blind spot you
could not close.
