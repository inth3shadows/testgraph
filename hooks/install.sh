#!/usr/bin/env bash
# install.sh — install testgraph's pre-push hook into every repo that has an
# approved journey registry (or into the repos named as arguments).
#
# Closes the gap issue #49 measured: the selector, the registries and the map all
# work, and nothing called them — `skills/testgraph-verify` was invoked 0 times
# across every session on this machine. A hook calls it whether or not anyone
# remembers to.
#
# Idempotent. Writes a marker-delimited block into the repo's SHARED hooks dir
# (`git rev-parse --git-common-dir`/hooks), so one install covers every worktree
# of that repo, and re-running replaces our block rather than stacking copies.
#
# It REFUSES a repo whose pre-push already belongs to someone else instead of
# appending to it — see the refusal below for why appending is worse than useless.
# Other hook types here (runecho-guard on pre-commit, autosync-hook-v2 on
# post-commit) are untouched; only pre-push is ours.
#
# Usage:
#   hooks/install.sh                       # every repo with an approved registry
#   hooks/install.sh /path/to/repo ...     # just these
#   hooks/install.sh --uninstall [repo...] # remove our block, leave the rest
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
PROJECTS="${PROJECTS_ROOT:-$HOME/personal_projects}"

# The path baked into the installed hook must be a STABLE checkout, not whichever
# worktree happened to run the installer: `claude-<ts>` worktrees are deleted on
# session exit, and a hook pointing at a deleted directory silently stops
# answering (it exits 0 by design, so nothing complains). Prefer the sibling
# `main` worktree. Install AFTER merging — a `main` that does not have
# testgraph/hook.py yet degrades to a silent no-op until it does.
TESTGRAPH_HOME="${TESTGRAPH_HOME:-}"
if [ -z "$TESTGRAPH_HOME" ]; then
    if [ "$(basename "$SRC")" != "main" ] && [ -d "$(dirname "$SRC")/main/testgraph" ]; then
        TESTGRAPH_HOME="$(dirname "$SRC")/main"
    else
        TESTGRAPH_HOME="$SRC"
    fi
fi
[ -f "$TESTGRAPH_HOME/testgraph/hook.py" ] || \
    echo "note: $TESTGRAPH_HOME has no testgraph/hook.py yet — the hook installs but stays a no-op until it does" >&2
OPEN="# >>> testgraph-hook-v1 >>>"
CLOSE="# <<< testgraph-hook-v1 <<<"

UNINSTALL=0
if [ "${1:-}" = "--uninstall" ]; then UNINSTALL=1; shift; fi

# Targets: explicit args, else every registry that a human has approved. An
# UNAPPROVED registry runs (loudly) by design, but auto-installing a hook that
# prints from one would put unreviewed journey names in front of a reader as if
# they were settled — approval is the line where the answer becomes quotable.
targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    while IFS= read -r name; do
        for wt in "$PROJECTS/$name/main" "$PROJECTS/$name"; do
            if [ -d "$wt/.git" ] || [ -f "$wt/.git" ]; then targets+=("$wt"); break; fi
        done
    done < <(python3 - "$SRC/journeys" <<'PY'
import glob, json, os, sys
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.json"))):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        continue
    if d.get("approved") and d.get("target"):
        print(d["target"])
PY
    )
fi

if [ ${#targets[@]} -eq 0 ]; then
    echo "no target repos found (no approved registry in $TESTGRAPH_HOME/journeys)" >&2
    exit 1
fi

for repo in "${targets[@]}"; do
    if ! common=$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null); then
        echo "skip $repo — not a git repo" >&2
        continue
    fi
    case "$common" in /*) ;; *) common="$repo/$common" ;; esac
    hooks_dir="$(cd "$common" && pwd)/hooks"
    mkdir -p "$hooks_dir"
    hook="$hooks_dir/pre-push"

    # Everything in the existing hook that is not our own block. Non-empty means
    # somebody else owns this file — see the refusal below.
    if [ -f "$hook" ]; then
        kept="$(awk -v o="$OPEN" -v c="$CLOSE" '
            $0 == o {skip=1; next} $0 == c {skip=0; next} !skip' "$hook")"
    else
        kept=""
    fi
    foreign="$(printf '%s\n' "$kept" | grep -v '^#!' | grep -v '^[[:space:]]*$' || true)"

    if [ "$UNINSTALL" -eq 1 ]; then
        if [ ! -f "$hook" ]; then
            continue
        elif [ -z "$foreign" ]; then
            rm -f "$hook"; echo "removed  $hook"
        else
            # Never delete a file we did not solely author.
            printf '%s\n' "$kept" > "$hook"; echo "unhooked $hook (other content kept)"
        fi
        continue
    fi

    # A foreign pre-push is REFUSED, not appended to. Appending looked free and is
    # not: a hook ending in `exit`/`exec` — the most ordinary way to write one, and
    # what this very template does — leaves our block after the exit, dead forever,
    # while the installer prints "installed". And a hook that ran `set -e` leaves it
    # set for our block, where one unset `git config wt.base` would fail the push
    # outright. Both were caught in review of this file's first version. Refusing is
    # loud and costs nothing today: no repo here has a pre-push hook at all. Solving
    # cohabitation properly means replaying stdin between blocks, which is real
    # complexity to buy a case that does not exist yet.
    if [ -n "$foreign" ]; then
        echo "skip $hook — a pre-push hook already exists here and is not ours." >&2
        echo "     Appending would leave testgraph's block after another manager's" >&2
        echo "     exit (silently dead) or under its \`set -e\` (a failed push)." >&2
        echo "     Merge it by hand, or move the existing hook aside first." >&2
        continue
    fi

    {
        echo "#!/usr/bin/env bash"
        echo ""
        echo "$OPEN"
        sed "s|__TESTGRAPH_HOME__|$TESTGRAPH_HOME|g" "$HERE/pre-push"
        echo "$CLOSE"
    } > "$hook"
    chmod +x "$hook"
    echo "installed $hook"
done
