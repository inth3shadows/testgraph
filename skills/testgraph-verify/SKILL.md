---
name: testgraph-verify
description: Before committing, name the user journeys your change could have broken. Reads a pre-computed journey map — no CodeGraph index, no Python, no MCP call needed at runtime. Use when you have edited product code and are about to commit, open a PR, or report work complete.
---

# testgraph-verify

You changed code. This tells you which end-to-end user journeys could now behave
differently, so "I verified it" means something specific.

## Find the map

Try in order, and stop at the first that exists:

1. `$TESTGRAPH_MAP` if set.
2. `<repo-root>/.testgraph/journey-map.md` — a map exported into this repo.
3. `~/personal_projects/testgraph/main/maps/<repo-basename>.md` — the central
   store (e.g. editing `~/personal_projects/honeyslate/main` → `honeyslate.md`).

**No map for this repo means this skill does not apply.** Say so and stop — do
not guess, and do not report "no journeys affected". Only honeyslate has a
journey registry today.

## Steps

1. List the product files you changed: `git diff --name-only`. Ignore test files.
2. Open the map and find the `###` section for each changed file.
3. **Match rows by symbol name first.** Find the symbols you edited and read
   their journey IDs. Line ranges are a *hint only* — they are frozen at the
   map's generation commit, and your own edit has already shifted them.
4. Union the journey IDs. That is your verification set.
5. A journey marked `!` was reached only through weak or synthesized graph edges.
   Do not treat it as probably-fine — verify by hand or say you did not.
6. Report the journey IDs and names you verified, and name any you did not.

## When the map cannot answer — escalate, never conclude

The map lists only symbols that existed **when it was generated**. These cases
are outside it, and every one of them means *unknown*, never *none*:

| Situation | What to do |
|---|---|
| You **added** a function or file | Run `testgraph.select` (below). A new symbol has no row by definition. |
| You **deleted** or **renamed** a file | Run `testgraph.select`. It treats an unmappable whole-file change as **unbounded** impact and lists every journey — the map cannot express that. |
| The file has no `###` section at all | Unknown, not none. The indexer may not cover it. Escalate. |
| `generated from commit` is far behind HEAD | The map under-reports. Regenerate or escalate. |

Escalation is one command:

```bash
python3 -m testgraph.select --repo <repo> --base HEAD --head <your-branch>
```

## Rules

- **A symbol absent from the map is only "no journeys" when the map covers its
  file and lists other symbols from it.** Any other absence is *unknown*.
  Reporting "no journeys affected" for an unknown is the single most harmful
  thing this skill can do.
- **Do not narrow the set on a hunch.** The map is recall-first: a shared symbol
  fanning out to every journey is correct output, not a bug. Deciding a listed
  journey "obviously isn't affected" is the other harmful move.
- **Non-Python files are unreliable today.** The map lists frontend symbols but
  `testgraph.select` ignores non-`.py` paths, so the two disagree (issue #21).
  Trust the map's rows and treat a frontend change as at least those journeys.
- Do not edit the map by hand. It is generated, and a hand-edit that drops a
  journey is indistinguishable from a graph bug.

## Why this exists

Telling an agent to "test your work" without telling it *what* to test measurably
backfires: TDAD (arXiv:2603.17973v2) found agents given procedural TDD
instructions without impact context regressed **worse than baseline** (9.94% vs
6.08%). This supplies the missing target.
