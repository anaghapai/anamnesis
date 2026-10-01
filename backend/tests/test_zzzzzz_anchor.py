"""Head anchor: deleting only the newest audit rows is now detected. Runs after every other test (changes the chain)."""
import json
import os

import pytest


@pytest.fixture()
def anchor_file():
    from app import auditchain
    return auditchain.ANCHOR_FILE


def _verify(client, H, **params):
    r = client.get("/audit/verify", headers=H["asha"], params=params)
    assert r.status_code in (200, 400), r.text
    return r


def test_first_verify_saves_head_outside_the_database(client, H, anchor_file):
    if os.path.exists(anchor_file):
        os.remove(anchor_file)
    j = _verify(client, H).json()
    assert j["ok"] is True and j["anchor"]["status"] == "created"
    assert j["head_full"] and len(j["head_full"]) == 64 and j["head_full"].startswith(j["head"])
    saved = json.load(open(anchor_file))
    assert any(v["head"] == j["head_full"] and v["count"] == j["checked"] for v in saved.values())
    assert not anchor_file.endswith(".db")


def test_second_verify_matches_and_new_rows_advance_the_anchor(client, H, anchor_file):
    j = _verify(client, H).json()
    assert j["ok"] and j["anchor"]["status"] == "matches"
    client.post("/auth/login", json={"identifier": "priya", "password": "demo1234"})   # writes a new audit row
    j2 = _verify(client, H).json()
    assert j2["ok"] and j2["anchor"]["status"] == "advanced" and j2["anchor"]["new_rows"] >= 1
    assert _verify(client, H).json()["anchor"]["status"] == "matches"


def test_deleting_only_the_newest_rows_is_detected(client, H, anchor_file):
    from app.db import SessionLocal
    from app import models
    assert _verify(client, H).json()["ok"] is True
    db = SessionLocal()
    try:
        rows = db.query(models.AuditLog).filter(models.AuditLog.row_hash.isnot(None)).order_by(models.AuditLog.id.desc()).limit(2).all()
        snaps = [dict(id=r.id, org_id=r.org_id, user_id=r.user_id, action=r.action, detail=r.detail,
                      created_at=r.created_at, prev_hash=r.prev_hash, row_hash=r.row_hash) for r in rows]
        for r in rows:
            db.delete(r)
        db.commit()
        j = _verify(client, H).json()
        # the remaining chain is internally consistent, so only the saved head can catch this
        assert j["ok"] is False and j["broken_at"] is None and "newest rows were removed" in j["reason"]
        assert j["anchor"]["status"] == "mismatch"
        for s in reversed(snaps):
            db.execute(models.AuditLog.__table__.insert().values(**s))
        db.commit()
        assert _verify(client, H).json()["ok"] is True
    finally:
        db.close()


def test_noted_head_from_outside_is_checked(client, H):
    j = _verify(client, H).json()
    ok = _verify(client, H, noted_head=j["head_full"][:20]).json()
    assert ok["ok"] and ok["noted"]["rows_after_it"] == 0
    bad = _verify(client, H, noted_head="0" * 20).json()
    assert bad["ok"] is False and "not in the chain" in bad["reason"]
    assert _verify(client, H, noted_head="xyz").status_code == 400


def test_corrupt_anchor_file_is_reported_not_overwritten(client, H, anchor_file):
    open(anchor_file, "w").write("{not json")
    j = _verify(client, H).json()
    assert j["ok"] is True and j["anchor"]["status"] == "unavailable"
    assert open(anchor_file).read() == "{not json"
    os.remove(anchor_file)
    assert _verify(client, H).json()["anchor"]["status"] == "created"
