import importlib
from app.svc import build

HOOK = "audit"

def create(name):
    payload = build(name)
    mod = importlib.import_module("app.dyn")
    return getattr(mod, HOOK)(payload)
