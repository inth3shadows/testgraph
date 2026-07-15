# Usage Guide: testgraph

## What this does

testgraph will answer one question for a code change: "which user-facing
flows are now at risk, and in what order should they be tested?" It reads the
change, consults a map of how the code connects to user journeys, and hands
back a short, ranked test plan instead of "re-run everything."

## Current status

Nothing to use yet — the project is in planning. This guide will be filled in
when the first `testgraph plan` command works against a real repo.

## Planned workflows

- Ask for a test plan after making changes (ranked list of at-risk flows).
- Record what was tested and what failed, so the next plan gets smarter.
- Automatic use: agent-driven merges consult testgraph before merging.

## Questions

Contact Eric (eric.minish@gmail.com).
