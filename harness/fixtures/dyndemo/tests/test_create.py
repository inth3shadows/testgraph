from app.routes import create

def test_create_audits():
    assert create("  Bob ") == {"name": "bob", "audited": True}
