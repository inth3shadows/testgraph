# dyndemo — a controlled target with one dynamic-dispatch edge

The smallest repo that can demonstrate the trace harness (issue #12) end to end,
and the smallest one that contains the defect the harness exists to find.

`app/routes.py` reaches `app/dyn.py:audit` through `getattr(mod, HOOK)`. No static
analysis resolves that, so CodeGraph records no edge to `audit`, so `audit` is
absent from journey J1's static footprint — while executing on every J1 request.
Edit `audit` and testgraph says nothing about J1.

Reproduce (needs a `python` with pytest, and `codegraph` on PATH — copy the
fixture out of the repo first, since it must be indexed in place):

```bash
cp -r harness/fixtures/dyndemo /tmp/dyndemo && cd /tmp/dyndemo
git init -q && git add -A && git commit -qm init
codegraph init /tmp/dyndemo
cd -

python3 harness/trace.py --repo /tmp/dyndemo --python <python-with-pytest> \
    --tests tests --root app --out /tmp/dyndemo-trace.json

python3 harness/ground_truth.py --trace /tmp/dyndemo-trace.json \
    --map /tmp/dyndemo/journey_tests.json \
    --registry /tmp/dyndemo/journeys.json \
    --db /tmp/dyndemo/.codegraph/codegraph.db
```

Expected (measured 2026-08-06, `sys.monitoring` backend, codegraph 1.4.1):

```
1/1 journey(s) scored; 1 traced symbol(s) outside the static footprint

  J1  create a thing  ! SILENT-MISS SOURCE
      traced 5 symbol(s) -> 4 node(s); static footprint 7 (from 1 resolved entry symbol(s))
      traced_only 1   static_only 4   unresolved 0
        - audit (app/dyn.py)
```

This is a *controlled* target, so it proves the instrument works — not that
honeyslate has this defect. That measurement is blocked; see TECHNICAL.md
"Update 4".
