"""Journey registry: a hand-authored map of user journeys to their entry
symbols. Names drift, node ids don't — so we store names + file and resolve to
node ids at run time.
"""
import json

from . import db as dbmod


def load(path):
    with open(path) as f:
        return json.load(f)


def resolve_entries(conn, registry):
    """entry_node_id -> journey_id. Maps ALL nodes matching an entry (name +
    file) so no definition of a handler is missed (recall-first)."""
    mapping = {}
    for jid, journey in registry["journeys"].items():
        for entry in journey["entries"]:
            ids = dbmod.resolve_symbol(conn, entry["name"], entry.get("file"))
            for nid in ids:
                mapping[nid] = jid
    return mapping


def unresolved(conn, registry):
    """[(journey_id, [unresolvable entry names])] for journeys with NO entry that
    resolves to a node in the index.

    A journey in this state can never be selected: `resolve_entries` yields
    nothing for it, so it silently disappears from every answer while the
    registry and the map legend still advertise it as covered. Rename a FastAPI
    handler without updating the registry and testgraph will report that no
    change can affect that journey. Callers must fail loud on a non-empty
    result — this is the registry-rot half of the drift problem (issue #19).
    """
    out = []
    for jid, journey in registry["journeys"].items():
        missing = [
            e["name"]
            for e in journey["entries"]
            if not dbmod.resolve_symbol(conn, e["name"], e.get("file"))
        ]
        if len(missing) == len(journey["entries"]):
            out.append((jid, missing))
    return out


def journey_name(registry, jid):
    return registry["journeys"][jid]["name"]
