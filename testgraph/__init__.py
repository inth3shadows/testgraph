"""testgraph — journey-level test selection over a CodeGraph index.

Phase 1 spike: given a git diff, emit the ranked set of user journeys whose
behavior could have changed, by traversing .codegraph/codegraph.db directly.
Recall-first: never silently drop a truly-affected journey; over-selection is
tolerated. See ~/.claude/plans/testgraph-phase1-graph-traversal-spike.md.
"""
# Version comes from the git tag via hatch-vcs, not a literal: the previous
# hardcoded "0.1.0-spike" was written during the Phase 1 spike and never updated
# again, so it reported a version the code had not been for months. Three sources,
# in order: the build-time generated file, then installed package metadata (a
# `pip install` of an sdist with no git dir), then an explicit unknown marker —
# never a stale number, because a wrong version is worse than an absent one.
try:  # built or installed from a wheel/sdist
    from ._version import __version__
except ImportError:  # pragma: no cover - raw source checkout
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("testgraph")
    except (ImportError, PackageNotFoundError):
        __version__ = "0+unknown"
