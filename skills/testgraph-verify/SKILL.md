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

1. List the product files you changed. Use `git diff --name-only HEAD` so
   **staged** work is included — plain `git diff` shows only unstaged changes and
   would report nothing after a `git add`. Ignore test files.
2. Open the map and find the `###` section for each changed file.
3. **Match rows by symbol name first.** Find the symbols you edited and read
   their journey IDs. Line ranges are a *hint only* — they are frozen at the
   map's generation commit, and your own edit has already shifted them.
   **Not every row is named for a function.** Import nodes (`sqlalchemy.orm`) and
   module-level bindings (`_settings`) get rows too, and you will not recognise
   them as "a symbol I edited". If you changed an import block, a module-level
   assignment, a decorator, or anything else you cannot attribute to a named row,
   use the line range as the fallback key and read every row it plausibly covers.
   An edit you cannot attribute to any row is *unknown* — the absence rule below
   does not apply to it.
4. Union the journey IDs. That is your verification set.
5. A journey marked `!` was reached only through weak or synthesized graph edges.
   Do not treat it as probably-fine — verify by hand or say you did not.
6. Report the journey IDs and names you verified, and name any you did not.

## When the map cannot answer — escalate, never conclude

The map lists only symbols that existed **when it was generated**. Every case
below means *unknown*, never *none*:

| Situation | What to do |
|---|---|
| You **added** a `.py` function or file | Run `select` (below). A new symbol has no row by definition. |
| You **deleted** or **renamed** a `.py` file | Run `select`. If the file is still in the index it seeds that file's symbols precisely and returns a *narrow* list — correct, not a malfunction. Only when the file has already left the index does it report unbounded impact and list every journey. |
| You changed **any non-`.py` file** (`.js`, `.jsx`, `.ts`, `.tsx`, `.svelte`, `.vue`, …) | Run `select` — it seeds these paths (issue #21; it used to answer `NONE` for all of them). If your extension is not in the index it prints `RECALL DEGRADED` and lists every journey: that is *unknown*, not none. A journey reached only through frontend code often arrives on a weak edge — expect `!` and verify by hand. |
| The file has no `###` section at all | Unknown. The indexer may not cover it. Escalate and say so. |
| `generated from commit` is far behind HEAD | The map under-reports. Regenerate (below) or escalate. |
| The stamp ends in **`-dirty`** | It was generated from a tree with uncommitted changes, so rows may describe code in no commit and the line hints are shifted further than usual. Regenerate after committing; until then treat a missing symbol as *unknown*. **Strip the `-dirty` suffix before comparing the stamp to a revision** — `<sha>-dirty` is not a valid git rev, and `git log <stamp>..HEAD` fails with `bad revision` rather than reporting no staleness. |

### Running select

`select` reads **committed history only** — it cannot see uncommitted edits, so
commit or stash first, then:

On a feature branch:

```bash
python3 -m testgraph.select --repo <repo> \
  --base "$(git -C <repo> merge-base main HEAD)" --head HEAD
```

Working directly on the default branch, `merge-base main HEAD` **is** `HEAD` and
the diff is empty — use the commit count instead:

```bash
python3 -m testgraph.select --repo <repo> --base HEAD~<N> --head HEAD
```

**Always confirm the range is non-empty before trusting the answer:**

```bash
git -C <repo> diff --name-only <base>..HEAD    # must list your files
```

Do **not** pass `--base HEAD --head <your-branch>` while checked out on that
branch, and do not use `merge-base` while on the default branch: both resolve to
the same commit, the diff is empty, and `select` prints
`journeys to test: NONE` — an answer that looks confident and means nothing.

### Regenerating a stale map

```bash
# into the repo it describes:
python3 -m testgraph.export --repo <repo> --into-target
# or into the central store:
python3 -m testgraph.export --repo <repo> --out maps/<target>.md
```

## Rules

- **A symbol absent from the map is only "no journeys" when the map covers its
  file and lists other symbols from it.** Any other absence is *unknown*.
  Reporting "no journeys affected" for an unknown is the single most harmful
  thing this skill can do.
- **Do not narrow the set on a hunch.** The map is recall-first: a shared symbol
  fanning out to every journey is correct output, not a bug. Deciding a listed
  journey "obviously isn't affected" is the other harmful move.
- **`NONE` from `select` is only trustworthy when the diff was non-empty and no
  `RECALL DEGRADED` line appeared.** Those are the two ways it says nothing while
  looking confident: an empty range (nothing to analyse) and a changed file with no
  symbols in the index (impact unbounded). Check both, then believe it.
- Do not edit the map by hand. It is generated, and a hand-edit that drops a
  journey is indistinguishable from a graph bug.

## Why this exists

Telling an agent to "test your work" without telling it *what* to test measurably
backfires: TDAD (arXiv:2603.17973v2) found agents given procedural TDD
instructions without impact context regressed **worse than baseline** (9.94% vs
6.08%). This supplies the missing target.
