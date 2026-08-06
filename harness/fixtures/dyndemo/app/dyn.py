def audit(payload):
    """Reached only by getattr dispatch below — no static call edge points here."""
    return dict(payload, audited=True)
