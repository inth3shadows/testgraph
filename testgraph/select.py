"""testgraph select — git diff -> ranked affected journeys.

Recall-first: the closure over-selects on shared-symbol changes rather than
risk dropping a truly-affected journey. Test, docs and type-declaration changes
are exempt (they don't change product behavior); any other path the index does
not model degrades the answer to every journey (issue #117).

Usage:
    python -m testgraph.select --repo <path> [--base SHA] [--head SHA]
                               [--db PATH] [--registry PATH] [--json]
"""
import argparse
import json
import os
import re
import subprocess
import sys

from . import db as dbmod
from . import integrity
from . import ledger
from . import registry as reg

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# Extensions CodeGraph indexes for this stack. Previously only `.py` was
# considered, so a change to any frontend file produced zero seeds and select
# answered "journeys to test: NONE" -- while `export`'s map, which walks all
# indexed nodes, listed those same files against J8. Two tools, different
# answers, and the map was the correct one (issue #21).
#
# Widening is not free, and the earlier claim that it "cannot invent impact" was
# only true for extensions the indexer does not cover (no nodes -> no seeds).
# Where the indexer DOES cover the extension it can add a false positive:
# measured mean precision fell 0.84 -> 0.68 when the frontend was seeded, via a
# 0.5-confidence edge to J8 (TECHNICAL.md). That path is flagged
# `verify_manually`, which is the intended trade -- recall stayed at 1.00.
PRODUCT_EXT = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
               ".cts", ".svelte", ".vue")

# Type declarations carry no runtime behavior and have no nodes, so seeding them
# cannot help; after #29 an extension-accepted file with no nodes degrades the
# whole answer to "test everything", so admitting them would be actively worse.
DECLARATION_EXT = (".d.ts", ".d.mts", ".d.cts")

# How many paths one warning names before "… N more" (#136).
MAX_LISTED = 5

# Directory names that mean "this is test code", matched as whole path segments.
# Substring matching on "/tests/" missed a repo-root `tests/` or `__tests__/`
# directory entirely, so root-level test files were seeded as product code.
TEST_DIRS = frozenset({"tests", "e2e", "__tests__"})

# Paths exempt from the unmodeled-path fallback (issue #117) because they cannot
# change runtime behavior: prose, by extension, by a top-level docs directory,
# or by the conventional file names — matched EXACTLY (optionally with a prose
# extension), because a prefix match exempted `db/authors.sql` and
# `config/licenses.json`. Deliberately short. Everything else outside
# PRODUCT_EXT -- SQL, Dockerfile, CI YAML, CSS, JSON, toml, requirements.txt,
# anything unrecognized -- is UNKNOWN impact, not "safe".
DOC_EXT = (".md", ".rst", ".adoc")
DOC_DIRS = frozenset({"docs", "doc"})
DOC_BASENAMES = frozenset({"LICENSE", "COPYING", "CHANGELOG", "NOTICE", "AUTHORS"})
DOC_BASENAME_EXT = ("", ".txt", ".md", ".rst", ".adoc")


def _is_product(path):
    return (
        path.endswith(PRODUCT_EXT)
        and not path.endswith(DECLARATION_EXT)
        and not _is_test(path)
    )


def _exemption(path):
    """Why a non-product path cannot change behavior, or None when it might.

    Only a test DIRECTORY exempts a non-code file. `_is_test`'s name rules
    (`test_*`, `*.test.*`, `*.spec.*`) are conventions for code; on other files
    they exempted `test_and_deploy.yml` and `openapi.spec.yaml`."""
    parts = path.split("/")
    if TEST_DIRS.intersection(parts[:-1]):
        return "test"
    if path.endswith(DECLARATION_EXT):
        return "type declaration"
    stem, ext = os.path.splitext(parts[-1])
    if (path.endswith(DOC_EXT) or (len(parts) > 1 and parts[0] in DOC_DIRS)
            or (stem.upper() in DOC_BASENAMES and ext.lower() in DOC_BASENAME_EXT)):
        return "docs"
    return None


def _unmodeled_kind(path):
    base = path.rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1]
    return f"{ext} file" if ext else base


def _is_test(path):
    parts = path.split("/")
    base = parts[-1]
    return (
        bool(TEST_DIRS.intersection(parts[:-1]))
        or base.startswith("test_")
        or base.endswith("_test.py")
        # JS/TS conventions, now that non-Python paths are seeded
        or ".test." in base
        or ".spec." in base
    )


_SIMPLE_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
                   "\\": 92, '"': 34}


def _unquote_git_path(p):
    """Decode a git C-quoted path (`"b/a\\"b.py"`, `"a/caf\\303\\251.py"`).

    git quotes a name containing `"`, `\\`, a control character, or (unless
    `core.quotePath=false`) a non-ASCII byte. An unquoted path is returned as is.
    Bytes are decoded as utf-8 with surrogateescape so a non-utf-8 name survives
    as a distinct string instead of raising."""
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out += c.encode("utf-8", "surrogateescape")
            i += 1
            continue
        n = body[i + 1]
        if n in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        else:
            out += (bytes([_SIMPLE_ESCAPES[n]]) if n in _SIMPLE_ESCAPES
                    else n.encode("utf-8", "surrogateescape"))
            i += 2
    return out.decode("utf-8", "surrogateescape")


def _header_path(raw):
    """Path from a '---'/'+++' line body: drop git's single trailing TAB (it marks
    a name containing a space), then undo C-quoting."""
    if raw.endswith("\t"):
        raw = raw[:-1]
    return _unquote_git_path(raw)


def _diff_git_path(rest):
    """Best-effort path from the tail of a `diff --git a/X b/Y` line, for a block
    that never produced a readable '+++' header. Returns None if it cannot say."""
    if '"' in rest:
        toks = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest)
        if len(toks) == 2:
            right = _unquote_git_path(toks[1])
            return right[2:] if right.startswith("b/") else None
        return None
    n = (len(rest) - 1) // 2
    left, right = rest[:n], rest[n + 1:]
    if len(rest) % 2 == 1 and left.startswith("a/") and right.startswith("b/"):
        return right[2:]
    return None


def _parse_unified_diff(diff, paths=None):
    """Parse a `git diff --unified=0` into
    `(ranges, whole_files)` where

      ranges      = {file: [(lo, hi), ...]}  changed line ranges, and
      whole_files = {path: reason}           files with no usable line ranges

    `whole_files` covers deletions and renames. Both used to vanish silently: a
    deleted file's '+++ ' header is '/dev/null', which is not a .py path, so the
    file was dropped and its dependents were never seeded — silent
    under-selection, the unsafe direction. A rename with no content edit
    produces no '@@' hunks at all, yet changes the module path for every
    importer.

    A '+++ ' line is only treated as a file header when it carries a 'b/' or
    '/dev/null' path, so a changed content line that renders as '+++ ...' is not
    misread as a header.

    Every `diff --git` line starts a fresh block (audit C1). Without the reset a
    block whose header could not be read left `cur` pointing at the PREVIOUS
    file, so its hunks were credited to the wrong file and its own file vanished.
    A product-looking block that ends with no recognisable '+++' header is
    reported in `whole_files` ("unparseable diff header") rather than dropped:
    unknown, not absent. The one exception is a block git marks as a new or
    deleted file without a header, which is an empty file — no content to seed,
    and a mode-only change (`old mode`/`new mode`, no '+++'), which is a chmod.

    `paths`, when given, is a set that collects EVERY path in the diff, product
    or not, before any filtering (issue #117): the caller accounts for each one
    instead of inferring "nothing changed" from what survived the filter.
    """
    ranges, whole_files, cur, prev = {}, {}, None, None
    blk = None  # state of the current `diff --git` block

    def note(path):
        if paths is not None and path:
            paths.add(path)

    def close_block():
        if blk:
            note(blk["path"])
        if (blk and not blk["header"] and not blk["rename"] and not blk["empty"]
                and not blk["mode"]):
            path = blk["path"]
            if path and _is_product(path):
                whole_files.setdefault(path, "unparseable diff header")

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            close_block()
            cur = prev = None
            blk = {"path": _diff_git_path(line[len("diff --git "):]),
                   "header": False, "rename": False, "empty": False,
                   "mode": False}
        elif line.startswith(("old mode ", "new mode ")):
            # A pure chmod: mode lines and no '+++' header. It changes no
            # content, so it is not an unreadable header (C1 follow-up).
            if blk:
                blk["mode"] = True
        elif line.startswith("--- "):
            p = _header_path(line[4:])
            prev = p[2:] if p.startswith("a/") else None
        elif line.startswith(("new file mode ", "deleted file mode ")):
            if blk:
                blk["empty"] = True
        elif line.startswith("rename from "):
            p = _unquote_git_path(line[len("rename from "):])
            note(p)
            if blk:
                blk["rename"] = True
            if _is_product(p):
                whole_files[p] = "renamed from"
        elif line.startswith("rename to "):
            p = _unquote_git_path(line[len("rename to "):])
            note(p)
            if blk:
                blk["rename"] = True
            if _is_product(p):
                whole_files[p] = "renamed to"
        elif line.startswith("+++ ") and (
            _header_path(line[4:]).startswith("b/")
            or _header_path(line[4:]) == "/dev/null"
        ):
            path = _header_path(line[4:])
            if blk:
                blk["header"] = True
            if path == "/dev/null":
                # whole-file deletion: the surviving path is on the '---' line
                note(prev)
                if prev and _is_product(prev):
                    whole_files[prev] = "deleted"
                cur = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            note(path)
            cur = path if _is_product(path) else None
            if cur is not None:
                ranges.setdefault(cur, [])
        elif cur is not None and line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                start = int(m.group(1))
                cnt = int(m.group(2) or 1)
                if cnt > 0:
                    ranges[cur].append((start, start + cnt - 1))
                else:
                    # Pure deletion (+N,0): no new lines, but behavior changed.
                    # Seed the enclosing node at the deletion boundary so the
                    # affected journey is still selected (recall-first).
                    lo = max(1, start)
                    ranges[cur].append((lo, lo + 1))
    close_block()
    return {f: r for f, r in ranges.items() if r}, whole_files


def changed_ranges(repo, base, head, paths=None):
    diff = subprocess.run(
        # Every flag pins something a user's git config could otherwise change
        # under us (audit C1): diff.noprefix / mnemonicPrefix drop the `b/` the
        # parser keys on, color.diff=always injects escapes into every line,
        # core.quotePath quotes non-ASCII names, external/textconv drivers
        # replace the patch text. A config-dependent parse reads as "no changes"
        # -- a confident, wrong NONE.
        # -M: detect renames so a moved module is seeded whole rather than read
        # as an unrelated delete + add.
        # --end-of-options: `base`/`head` are never parsed as git options (M2).
        ["git", "-C", repo, "-c", "core.quotePath=false", "diff", "--no-color",
         "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
         "--unified=0", "-M",
         # diff.ignoreSubmodules, a .gitmodules `ignore`, or diff.submodule=log
         # drop or reshape a submodule bump so no `diff --git` block names it
         # (#117 review): pin both so the path is always accounted for.
         "--submodule=short", "--ignore-submodules=none",
         "--end-of-options", f"{base}..{head}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=True,
    ).stdout
    return _parse_unified_diff(diff, paths)


def diff_base(repo, base, head):
    """The revision to diff `head` against: `merge-base(base, head)`, or `None`
    when the two share no history (git exits 1). Any other git failure (a bad
    revision, a missing object) raises, rather than reading as "unrelated".

    `base..head` compared two trees, so on a branch behind `base` the changes
    that landed on `base` since the fork were read, reversed, as this branch's
    own (issue #112): a docs-only branch listed every journey because main had
    added a migration. The merge-base is what `git diff base...head` means.
    When `base` is an ancestor of `head` (HEAD~1) it IS `base`, so nothing
    changes there.

    Not for a push: a push moves the remote from its old tip to the new one,
    and after a rebase or a rewind the fork point hides what the push removed.
    The hook asks for the exact range (`select(..., exact_range=True)`)."""
    proc = subprocess.run(
        ["git", "-C", repo, "merge-base", "--end-of-options", base, head],
        capture_output=True, text=True,
    )
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args,
                                            proc.stdout, proc.stderr)
    return proc.stdout.strip() or None


def select(repo, base, head, db_path, registry_path, strict_registry=True,
           exact_range=False):
    # A revision starting with '-' is an option, not a revision (audit M2):
    # `--output=<path>` made `git diff` truncate an arbitrary file and return an
    # empty diff -- a clean NONE. Reachable from the MCP tool, where the model
    # supplies base/head. `--end-of-options` in changed_ranges is the second
    # layer; this refuses before ANY git call and says why.
    for label, rev in (("base", base), ("head", head)):
        if str(rev).startswith("-"):
            raise ValueError(f"revision may not start with '-': {label}={rev!r}")
    conn = dbmod.connect(db_path)
    try:
        registry = reg.load(registry_path)

        blocking, warnings = integrity.check(
            conn,
            repo,
            registry.get("spot_checks", {}),
            schema_pin=registry.get("codegraph_schema_version"),
            extraction_pin=registry.get("codegraph_extraction_version"),
        )
        # Provenance of the registry itself. Everything below checks whether the
        # registry AGREES with the code; this checks whether anyone ever read it. A
        # machine-drafted registry (issue #6) is runnable on purpose, so this is a
        # warning, not a block -- but a `NONE` answer computed from an unreviewed
        # registry means "not registered", not "not affected".
        approval = reg.approval_warning(registry)
        if approval:
            warnings.append(approval)

        # A journey whose entries do not resolve can never be selected -- silent
        # under-selection. Blocking is right for live use. But when ANALYSING HISTORY
        # (the accuracy harness checks out old commits) a journey that simply did not
        # exist yet is expected, not rot, and blocking would just shrink the scored
        # set. `strict_registry=False` downgrades it to a reported field so the
        # distinction is explicit rather than accidental.
        unresolved = [jid for jid, _ in reg.unresolved(conn, registry)]
        if unresolved:
            detail = ", ".join(
                f"{jid} ({reg.journey_name(registry, jid)})" for jid in unresolved
            )
            if strict_registry:
                blocking.append(
                    f"journeys with no resolvable entry symbol: {detail} — registry is "
                    f"stale against the index; they can never be selected"
                )
            else:
                warnings.append(
                    f"journeys absent from this index (not scored): {detail}"
                )

        blocking.extend(reg.trigger_problems(registry))

        # Drift the index cannot see: the registry and the index can agree while both
        # are stale against the source. A live parse is the only check in this pipeline
        # that reads the working tree, so it is the only one that catches a handler
        # renamed since the last `codegraph index` (issue #7). Reported as a
        # first-class field AND a warning, never blocking — see registry.live_drift.
        drift = reg.live_drift(repo, registry)
        for jid, name, rel, why in drift:
            # Remedy per reason, not one hard-coded "re-index". live_drift reads the
            # WORKING TREE while everything else here reads committed history, so an
            # uncommitted rename — the common case — would otherwise send an agent off
            # to rebuild an index that cannot change this answer.
            warnings.append(
                f"journey {jid} entry `{name}` ({rel}): {why} — {reg.remedy_for(why)}"
            )
        # Entries no parser covers are NOT drift and must not ride the warning channel:
        # re-indexing can never clear them, so a permanent warning would train the
        # reader to ignore warnings. Reported as their own field instead.
        unchecked = reg.unchecked_entries(registry)

        result = {"base": base, "head": head, "warnings": warnings,
                  "unresolved_journeys": unresolved,
                  "entry_drift": [
                      {"journey": jid, "entry": name, "file": rel, "reason": why}
                      for jid, name, rel, why in drift
                  ],
                  "entries_unchecked": [
                      {"journey": jid, "entry": name, "file": rel}
                      for jid, name, rel in unchecked
                  ]}
        if blocking:
            result["status"] = "BLOCKED"
            result["blocking"] = blocking
            return result

        diff_paths = set()
        merge_base = None if exact_range else diff_base(repo, base, head)
        head_sha = ledger.resolve_commit(repo, head)
        if merge_base is not None and merge_base == head_sha != ledger.resolve_commit(repo, base):
            # head is BEHIND base: the fork point is head itself and the diff
            # would be empty by construction — a confident NONE for whatever
            # base has that head does not (review of #112). The tree diff
            # shows it, reversed: over-selection, the safe direction.
            warnings.append(
                f"{head} is behind {base} (an ancestor of it) — compared the two "
                f"trees directly, so this lists {base}'s changes in reverse"
            )
            merge_base = None
        elif merge_base is None and not exact_range:
            # Unrelated histories or a shallow clone: no fork point to diff from.
            # The tree diff over-selects, which is the safe direction. An
            # unknown revision still fails loudly in `changed_ranges` below.
            warnings.append(
                f"{base} and {head} have no common ancestor (unrelated histories "
                f"or a shallow clone) — compared the two trees directly, which "
                f"may include changes that are not {head}'s"
            )
        ranges, whole_files = changed_ranges(repo, merge_base or base, head, diff_paths)

        # A changed file whose hunks resolve to NO node is the same failure as an
        # unmappable whole-file change, and it used to pass silently: seeds stayed
        # empty, no warning was raised, and the answer was a confident
        # "journeys to test: NONE" (issue #29). It happens whenever the index
        # predates the file (a newly added module) or covers the extension in
        # PRODUCT_EXT but not in this repo's graph. Per-file node sets, not one
        # running total, so one mapped file cannot mask an unmapped one.
        unmapped = []
        unmapped_files = set()

        def _mark_unmapped(path, detail):
            # `unmapped_files` gates the confinement check below (an untrusted
            # seed must not read as evidence of confinement) — one helper instead
            # of three independent append/add pairs means that gate can't drift
            # out of sync with `unmapped` itself.
            unmapped.append(f"{path} ({detail})")
            unmapped_files.add(path)

        seeds = set()
        seeds_by_file = {}
        for f, rs in sorted(ranges.items()):
            in_file = set()
            for lo, hi in rs:
                in_file.update(dbmod.nodes_for_lines(conn, f, lo, hi))
            if in_file:
                seeds.update(in_file)
                seeds_by_file[f] = in_file
            else:
                _mark_unmapped(f, "changed lines map to no indexed symbol")

        # Whole-file changes (deletions, renames) have no line ranges to map: seed
        # every symbol the file contains. A file deleted in `head` is usually absent
        # from an index built at `head`, so this resolves only when the index still
        # predates the deletion (e.g. the per-commit harness). When it does not
        # resolve, impact is UNBOUNDED — we cannot know what depended on it — and
        # recall-first means saying so loudly rather than returning a narrow answer.
        for path, reason in whole_files.items():
            nodes = dbmod.nodes_in_file(conn, path)
            if nodes:
                seeds.update(nodes)
                # A rename that ALSO carries edited hunks lands in both `ranges`
                # and `whole_files` for the new path. `seeds` above always gets
                # the full file (recall-first: a rename changes the module path
                # for every importer, so the whole file is in play regardless of
                # which lines moved) — but for the confinement check specifically,
                # letting the broader whole-file set clobber a precise range-based
                # entry can mask a real issue-#63 confinement in the lines that
                # actually changed behind unrelated untouched symbols that happen
                # to reach elsewhere. Keep the narrower, already-set entry.
                if path not in seeds_by_file:
                    seeds_by_file[path] = set(nodes)
            else:
                _mark_unmapped(path, reason)

        # A changed path the index does not model at all — SQL, a Dockerfile, CI
        # YAML, CSS — never becomes a seed, and used to vanish in the parser: a
        # migration-only diff answered "0 changed files, NONE" with no warning
        # (issue #117). Not indexed is not "no impact". Unless the path is
        # exempt (tests, docs, type declarations), it degrades the answer to
        # every journey, exactly like a code file with no symbols (#29).
        #
        # A registry `file_triggers` glob (issue #118) is the exception: it maps
        # matched paths straight to journeys, additively, and is the registry's
        # claim of complete coverage for them, so a matched unmodeled path does
        # not force the fallback. Every raw path is matched, product and exempt
        # included, so a trigger is never hidden by the filter.
        triggers = registry.get("file_triggers") or {}
        trigger_hits = []
        triggered = {}  # jid -> [{"pattern", "path"}]
        for path in sorted(diff_paths):
            for pattern, jids in triggers.items():
                if reg.trigger_matches(pattern, path):
                    trigger_hits.append(
                        {"path": path, "pattern": pattern, "journeys": list(jids)})
                    for jid in jids:
                        triggered.setdefault(jid, []).append(
                            {"pattern": pattern, "path": path})
        trigger_covered = {t["path"] for t in trigger_hits}

        exempt_files, unmodeled_files, untriggered = {}, [], []
        for path in sorted(diff_paths):
            if _is_product(path):
                continue
            why = _exemption(path)
            if why:
                exempt_files[path] = why
            else:
                unmodeled_files.append(path)
                if path not in trigger_covered:
                    untriggered.append(path)

        # A changed file that is NEWER than its index row is a third way to be
        # unmappable, and the quietest. The other two resolve to no node and are
        # obvious; this one resolves to the WRONG node. Seeds come from line ranges
        # (`nodes_for_lines`), so a file that gained twenty lines above a function
        # since it was indexed hands the diff's line numbers to whatever symbol used
        # to live there — a neighbouring function, or nothing. The answer stays
        # confident and can be narrower than the truth, which is the one failure
        # this selector is built to refuse.
        #
        # The seeds computed above are KEPT, not discarded: whatever they resolved to
        # is still evidence, and recall-first means adding doubt, not removing rows.
        # Joining `unmapped` degrades the answer to "every journey, verify manually"
        # — the same treatment issue #29 established for a file with no symbols.
        #
        # The `pre-push` hook runs `codegraph sync` first precisely so this stays the
        # exceptional path rather than every push.
        drifted = integrity.content_drift(conn, repo, set(ranges) | set(whole_files))
        for path in sorted(drifted):
            _mark_unmapped(path, "bytes differ from the indexed copy — line spans are stale")

        impacted, impacted_caps = dbmod.impacted_closure(conn, seeds, with_reasons=True)
        entry_map = reg.resolve_entries(conn, registry)

        # A closure that resolves fine but never leaves the file(s) its seeds
        # started in is a second, silent way to be blind (issue #63) — distinct
        # from `unmapped` above (no node at all). The seeds have symbols; those
        # symbols just have no recorded outbound reach. Checked per file, not
        # against the union of every seeded file in this diff, so a genuinely
        # cross-file change cannot mask a same-diff file that stayed confined.
        # Files already in `unmapped` are skipped: their seeds are untrusted, not
        # evidence of confinement.
        #
        # NOT flagged when the file's own closure already lands on a registered
        # entry point (`entry_map`): a route handler with no callers on record is
        # not a blind spot, it's a correctly-confident answer — the seed IS the
        # thing the registry names, so there is nothing "unknown" left to say.
        # Reproduced against this repo's own index before this guard existed: 7
        # of 11 "confined" files had already selected a journey at confidence
        # 1.0, and the warning called that UNKNOWN anyway.
        #
        # One recursive traversal per seeded file (beyond the single-file reuse
        # below), and `closure_files` scans every resulting node rather than
        # short-circuiting on the first one outside `f`: a mechanical
        # rename/refactor touching many files pays for it on every one. Accepted
        # for the same reason `journeys` already loops `caller_edge_count` per
        # entry above — this selector is recall-first and already spends
        # per-item DB round trips elsewhere; a diff wide enough to feel this is
        # also wide enough to be a whole-file/`unmapped` case on the commonest
        # paths. Revisit if this ever shows up in profiling.
        confined_files = []
        for f, file_seeds in sorted(seeds_by_file.items()):
            if f in unmapped_files:
                continue
            # Judged per SEED, not per file: a file can carry both a registered
            # entry (correctly confident on its own) and an unrelated edited
            # symbol that is the actual blind spot. Clearing the whole file
            # because ANY of its seeds happens to be an entry point silently
            # swallowed the NOTE for the seed that needed it — reproduced with
            # two unrelated edited symbols in one file, one an entry, one not.
            uncovered = file_seeds - entry_map.keys()
            if not uncovered:
                continue
            # `impacted` is already this closure whenever the uncovered seeds are
            # the full seed set — reuse it instead of re-running the same
            # recursive traversal. Checked by value, not by file/seed counts:
            # inferring it silently breaks if a future seed source desyncs from
            # `seeds`.
            file_impacted = (
                impacted if uncovered == seeds else dbmod.impacted_closure(conn, uncovered)
            )
            # `file_impacted` always contains at least the seeds themselves
            # (`impacted_closure` seeds every id at 1.0), and every seed in
            # `uncovered` came from a `nodes` row whose own `file_path == f` — so
            # `closure_files` here can never come back empty, UNLESS the closure
            # reached an id with no matching `nodes` row (a dangling edge / stale
            # index), which `closure_files` reports as `None` rather than
            # silently reading as "resolves to no other file". An index
            # inconsistent enough to produce that is not evidence of anything.
            reached_files = dbmod.closure_files(conn, file_impacted.keys())
            if reached_files is not None and reached_files <= {f}:
                confined_files.append(f)

        touched = {}
        for nid in impacted.keys() & set(entry_map):
            for jid in entry_map[nid]:
                touched.setdefault(jid, set()).add(nid)

        journeys = []
        for jid, ents in touched.items():
            fanin = sum(dbmod.caller_edge_count(conn, e) for e in ents)
            # Strongest route into the journey: if ANY entry is reached confidently,
            # the selection is trustworthy.
            best = max(ents, key=lambda e: impacted[e])
            conf = impacted[best]
            journeys.append(
                {
                    "id": jid,
                    "name": reg.journey_name(registry, jid),
                    "entries_hit": len(ents),
                    "rank": fanin,
                    "confidence": round(conf, 3),
                    "verify_manually": conf <= dbmod.LOW_CONFIDENCE,
                }
            )
            # A flag nobody can act on is noise. Say which kind of weak edge held
            # the surviving route down, so the reader knows whether to distrust the
            # graph (name collision) or the runtime (synthesized dispatch).
            #
            # Deliberately NOT the `reason` key: that one means "why this row is
            # here at all" and appears only on the bare degrade rows below, whose
            # `entries_hit` is 0. Two tests read its ABSENCE as proof a journey was
            # genuinely selected by the closure, so reusing it would quietly make a
            # real selection look like a degrade row.
            weak_reason = dbmod.weak_edge_reason(impacted_caps.get(best, ""))
            if weak_reason and journeys[-1]["verify_manually"]:
                journeys[-1]["weak_reason"] = weak_reason
        # Declared by a file trigger: a graph row keeps its graph meaning (no
        # `reason`) and gains `triggered_by`; a trigger-only journey gets its own
        # confident row — a human declared the dependency — labelled
        # `file-trigger` so it never reads as a graph edge.
        by_id = {j["id"]: j for j in journeys}
        for jid in sorted(triggered, key=reg.journey_sort_key):
            if jid in by_id:
                by_id[jid]["triggered_by"] = triggered[jid]
            else:
                journeys.append(
                    {
                        "id": jid,
                        "name": reg.journey_name(registry, jid),
                        "entries_hit": 0,
                        "rank": 0,
                        "confidence": 1.0,
                        "verify_manually": False,
                        "reason": "file-trigger",
                        "triggered_by": triggered[jid],
                    }
                )
        # Unmappable whole-file change -> unbounded impact. Add every journey the
        # closure did not already select, flagged for manual verification, so the
        # answer degrades toward "test everything" instead of toward silence.
        if unmapped:
            warnings.append(
                f"{len(unmapped)} changed file(s) the index cannot be trusted for "
                f"({', '.join(unmapped)}) — impact is unbounded; all journeys listed"
            )
        # Its own warning and its own row reason (#138): re-indexing can never
        # change the answer for a file the index does not model, so the #29
        # wording would send the reader the wrong way. The path list is capped
        # (#136); `unmodeled_files` carries all of them.
        if untriggered:
            shown = ", ".join(untriggered[:MAX_LISTED])
            if len(untriggered) > MAX_LISTED:
                shown += f", … {len(untriggered) - MAX_LISTED} more"
            warnings.append(
                f"{len(untriggered)} changed path(s) not modeled by the index and "
                f"matched by no file trigger ({shown}) — impact unknown; all "
                f"journeys listed"
            )
        if unmapped or untriggered:
            fallback_reason = ("change with no resolvable symbols" if unmapped
                               else "path the index does not model")
            selected = {j["id"] for j in journeys}
            for jid in sorted(registry.get("journeys", {}), key=reg.journey_sort_key):
                if jid not in selected:
                    journeys.append(
                        {
                            "id": jid,
                            "name": reg.journey_name(registry, jid),
                            "entries_hit": 0,
                            "rank": 0,
                            "confidence": 0.0,
                            "verify_manually": True,
                            "reason": fallback_reason,
                        }
                    )

        journeys.sort(key=lambda j: (-j["rank"], reg.journey_sort_key(j["id"])))

        # Not appended to `warnings`: this is its own signal, deliberately kept
        # off the capped, generic channel. `_render` below and `hook.render` each
        # give it a dedicated, uncapped line (`closure_confined` on the result is
        # the data both read from) — riding `warnings` would let it silently drop
        # off a push whose other warnings already filled hook.py's MAX_WARNINGS.

        if merge_base is not None and merge_base != ledger.resolve_commit(repo, base):
            result["diff_base"] = merge_base
        result.update(
            status="OK",
            changed_files=sorted(ranges),
            diff_files=sorted(diff_paths),
            exempt_files=exempt_files,
            unmodeled_files=unmodeled_files,
            file_triggers=trigger_hits,
            whole_file_changes=whole_files,
            recall_degraded=bool(unmapped or untriggered),
            closure_confined=confined_files,
            seed_symbols=len(seeds),
            impacted_symbols=len(impacted),
            journeys=journeys,
        )
        result["unknown_because"] = none_is_unknown(result)
        return result
    finally:
        # Close deterministically (audit M6): a long-lived MCP server calls this per
        # request and would otherwise hold one sqlite handle per call until GC.
        conn.close()


def none_is_unknown(result):
    """Reasons an empty journey list must be read as UNKNOWN, not as safe.

    `journeys: []` is a claim about the PRODUCT — "nothing you changed reaches a
    registered behavior". It is only that claim when nothing is telling the tool
    its own input may be wrong. When something is, the honest answer is a claim
    about the TOOL: it found nothing, and it cannot vouch for having looked
    properly (issue #75).

    Found by dogfooding: the pre-push hook printed `no journeys selected (no
    product-behavior change detected)` on the commit that added `mcp.py` — a new
    module, a new CLI surface, a new journey — with a staleness warning two lines
    below it. Post-`codegraph sync` the same query returned 22 seeds and J7. The
    warning fired; the headline still read as a clean bill.

    Returns a list of short reason strings, empty when the NONE is trustworthy.
    Non-empty is the signal; the strings are for display.

    Every warning that can COEXIST with an empty journey list is a trust signal,
    so `warnings` is taken wholesale rather than pattern-matched. The one warning
    that is not — unbounded impact — appends every journey in the registry, so it
    can never reach here. Being conservative in this direction is the recall-first
    choice: a false UNKNOWN costs a re-read, a false ALL-CLEAR costs the bug.

    Deliberately NOT blocking, and deliberately not a `warnings` entry.
    `integrity.py` argues at length that mtime staleness is wrong often enough
    (any `git checkout`) that nothing may be DECIDED on it — this decides nothing
    and changes no exit code. It only stops the renderer from making a claim the
    result does not support.
    """
    if result.get("journeys"):
        return []
    reasons = []
    if result.get("warnings"):
        n = len(result["warnings"])
        reasons.append(
            f"{n} trust warning(s) above — the index may not see this change"
        )
    if result.get("closure_confined"):
        reasons.append(
            f"{len(result['closure_confined'])} changed file(s) whose impact "
            f"never left the file it started in"
        )
    if result.get("entries_unchecked"):
        reasons.append(
            f"{len(result['entries_unchecked'])} journey entry symbol(s) never "
            f"verified against source — no parser for that file type"
        )
    return reasons


def _render(result):
    lines = [f"base..head: {result['base']}..{result['head']}"]
    for w in result.get("warnings", []):
        lines.append(f"  WARN: {w}")
    # visible without --json: a CLI user could otherwise not tell that some entry
    # symbols were never checked against source
    for u in result.get("entries_unchecked", []):
        lines.append(
            f"  NOTE: {u['journey']} entry `{u['entry']}` ({u['file']}) not "
            f"verified against source — no parser for that file type"
        )
    if result["status"] == "BLOCKED":
        lines.append("STATUS: BLOCKED — index not trustworthy")
        for b in result["blocking"]:
            lines.append(f"  x {b}")
        return "\n".join(lines)
    changed = f"changed files: {len(result['changed_files'])}"
    if "diff_files" in result:
        changed += (
            f" product of {len(result['diff_files'])} in diff "
            f"({len(result.get('exempt_files', {}))} exempt, "
            f"{len(result.get('unmodeled_files', []))} unmodeled)"
        )
    if result.get("diff_base"):
        lines.append(f"  diffed from merge-base {result['diff_base'][:12]} "
                     f"({result['base']} has moved on since {result['head']} forked)")
    lines.append(
        f"{changed} | "
        f"seeds: {result['seed_symbols']} | impacted symbols: {result['impacted_symbols']}"
    )
    for path, reason in sorted(result.get("whole_file_changes", {}).items()):
        lines.append(f"  whole-file: {path} ({reason})")
    for t in result.get("file_triggers", []):
        lines.append(f"  trigger: {t['path']} matches `{t['pattern']}` → "
                     f"{', '.join(t['journeys'])}")
    covered = {t["path"] for t in result.get("file_triggers", [])}
    for path in result.get("unmodeled_files", []):
        if path not in covered:
            lines.append(f"  unmodeled: {path} — not in the index, impact UNKNOWN")
    if result.get("recall_degraded"):
        lines.append("  RECALL DEGRADED — unbounded impact, all journeys listed")
    for f in result.get("closure_confined", []):
        lines.append(
            f"  NOTE: impact for {f} did not leave the file it started in — "
            f"either the module is genuinely leaf-only, or its callers are "
            f"not linked in the index; that file's own contribution is "
            f"UNKNOWN, not verified-safe"
        )
    if not result["journeys"]:
        # Two different sentences, because they are two different claims. See
        # `none_is_unknown`.
        unknown = result.get("unknown_because") or none_is_unknown(result)
        if unknown:
            lines.append(
                "journeys to test: NONE — but UNKNOWN, not verified-safe:"
            )
            for reason in unknown:
                lines.append(f"    {reason}")
        else:
            lines.append(
                "journeys to test: NONE (no product-behavior change detected)"
            )
    else:
        lines.append(f"journeys to test ({len(result['journeys'])}), ranked:")
        for j in result["journeys"]:
            why = j.get("weak_reason") or j.get("reason") or "weak edge path"
            flag = "  ! VERIFY MANUALLY — %s" % why if j["verify_manually"] else ""
            if j.get("reason") == "file-trigger":
                flag = "  — file-trigger"
            lines.append(
                f"  [{j['rank']:>3}] {j['id']}  {j['name']}  "
                f"({j['entries_hit']} entry, conf {j['confidence']}){flag}"
            )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testgraph.select")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--db", default=None, help="defaults to <repo>/.codegraph/codegraph.db")
    ap.add_argument(
        "--registry",
        default=None,
        help="defaults to the journeys/*.json whose `target` matches <repo>",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exact-range", action="store_true",
                    help="diff base..head as two trees, not from their merge-base "
                         "(what a push changes; the pre-push hook uses this)")
    args = ap.parse_args(argv)

    db_path = args.db or os.path.join(args.repo, ".codegraph", "codegraph.db")
    # Resolve by the registry's own `target`, and REFUSE rather than fall back.
    # This used to default to honeyslate's registry unconditionally, so pointing
    # --repo at any other project loaded the wrong journeys and then reported the
    # resulting disagreement as a stale index — a confidently wrong diagnosis.
    registry_path = args.registry or reg.resolve_for_repo(args.repo)
    if registry_path is None:
        print(reg.not_found_message(args.repo), file=sys.stderr)
        return 2
    try:
        result = select(args.repo, args.base, args.head, db_path, registry_path,
                        exact_range=args.exact_range)
    except dbmod.IndexMissing as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_render(result))
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
