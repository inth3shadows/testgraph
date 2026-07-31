"""Shared git-worktree + CodeGraph-index lifecycle for the harness scripts.

Every harness that faithfully simulates "testgraph running at commit C" needs
the same sequence: check C out into an ISOLATED worktree (so it cannot collide
with another commit's checkout), build a FRESH index there (so line spans and
`files.content_hash` agree with what is actually on disk at C), then tear the
worktree down whether the run succeeded or not. `accuracy.py`, `selectivity.py`
and `seed_regressions.py` each grew a hand-rolled copy of this. Extracted here
so the next harness reuses a version that already got the ordering right,
rather than re-deriving it — and re-risking the content-hash mismatch #52
exists to catch — from scratch.

Leading underscore: this is harness-internal plumbing, not a public API any
target repo or registry depends on.
"""
import os
import subprocess


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


class WorktreeError(Exception):
    """A worktree add or index build failed. `.stage` is `"WORKTREE-FAIL"` or
    `"INDEX-FAIL"` — callers use it to decide whether a worktree exists to
    clean up (add failure: nothing was created; index failure: the worktree
    is there and still needs `remove`)."""

    def __init__(self, stage, detail):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}")


def add_and_index(bare, sha, dest, codegraph_bin):
    """Check `sha` out into `dest` (a fresh detached worktree of `bare`) and
    build a full CodeGraph index there. Returns the index db path.

    Raises `WorktreeError` on failure rather than returning a sentinel — every
    caller here scores or logs a per-commit row differently, so the decision
    of WHAT to record belongs to the caller, not this function. Does not clean
    up on failure: `remove` is the caller's job (see `WorktreeError.stage` for
    why), matching each harness's existing per-row bookkeeping.
    """
    add = run(["git", "-C", bare, "worktree", "add", "--detach", dest, sha])
    if add.returncode:
        raise WorktreeError("WORKTREE-FAIL", add.stderr.strip())
    idx = run([codegraph_bin, "init", dest])
    db = os.path.join(dest, ".codegraph", "codegraph.db")
    if not os.path.exists(db):
        raise WorktreeError("INDEX-FAIL", idx.stderr.strip())
    return db


def remove(bare, dest):
    run(["git", "-C", bare, "worktree", "remove", "--force", dest])
