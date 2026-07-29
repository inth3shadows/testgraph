---
name: testgraph-verify
description: Before committing, name the user journeys your change could have broken. Reads a pre-computed journey map — no CodeGraph index, no Python, no MCP call needed at runtime. Use when you have edited product code and are about to commit, open a PR, or report work complete.
---

# testgraph-verify

You changed code. This tells you which end-to-end user journeys could now behave
differently, so "I verified it" means something specific.

## Steps

1. List the product files you changed (`git diff --name-only`). Ignore test files.
2. Open the journey map for this repo: `maps/<target>.md`.
3. For each changed file, find its `###` section and read the rows whose line
   range overlaps your edit.
4. Union the journey IDs from those rows. That is your verification set.
5. A journey marked `!` was reached only through weak or synthesized graph edges.
   Do not treat it as probably-fine — verify it by hand or say you did not.
6. Report the journey IDs and names you verified, and explicitly name any you
   did not.

## Rules

- **Do not narrow the set on a hunch.** The map is recall-first: a shared symbol
  fanning out to every journey is correct output, not a bug. Deciding a listed
  journey "obviously isn't affected" is the one move that makes this harmful.
- **A symbol absent from the map reaches no journey.** That is a real answer —
  report "no journeys affected", not "unknown".
- **If the map's `generated from commit` is far behind HEAD, say so.** A stale
  map under-reports. Regenerate with
  `python3 -m testgraph.export --repo <path> --out maps/<target>.md`.
- Do not edit the map by hand. It is generated, and a hand-edit that drops a
  journey is indistinguishable from a graph bug.

## Why this exists

Telling an agent to "test your work" without telling it *what* to test measurably
backfires: TDAD (arXiv:2603.17973v2) found agents given procedural TDD
instructions without impact context regressed **worse than baseline** (9.94% vs
6.08%). This supplies the missing target.
