"""testgraph — journey-level test selection over a CodeGraph index.

Phase 1 spike: given a git diff, emit the ranked set of user journeys whose
behavior could have changed, by traversing .codegraph/codegraph.db directly.
Recall-first: never silently drop a truly-affected journey; over-selection is
tolerated. See ~/.claude/plans/testgraph-phase1-graph-traversal-spike.md.
"""
__version__ = "0.1.0-spike"
