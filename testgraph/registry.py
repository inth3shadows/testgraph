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


def journey_name(registry, jid):
    return registry["journeys"][jid]["name"]
