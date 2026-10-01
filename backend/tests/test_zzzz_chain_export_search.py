"""Hash-chained audit log, CSV export and the timezone-aware search date filter. Runs last."""
import datetime


def test_new_rows_are_chained_and_verify_ok(client, H):
    r = client.get("/audit/verify", headers=H["asha"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["checked"] > 5 and j["broken_at"] is None and j["head"]


def test_verify_is_managers_only(client, H):
    assert client.get("/audit/verify", headers=H["priya"]).status_code == 403
    assert client.get("/audit/export.csv", headers=H["kabir"]).status_code == 403
    assert client.get("/audit/verify").status_code in (401, 403)


def test_editing_or_deleting_a_row_is_detected(client, H):
    from app.db import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        rows = db.query(models.AuditLog).filter(models.AuditLog.row_hash.isnot(None)).order_by(models.AuditLog.id).all()
        mid = rows[len(rows) // 2]
        old = mid.detail
        mid.detail = (old or "") + " TAMPERED"
        db.commit()
        j = client.get("/audit/verify", headers=H["asha"]).json()
        assert j["ok"] is False and j["broken_at"] == mid.id and "edited" in j["reason"]
        mid.detail = old
        db.commit()
        assert client.get("/audit/verify", headers=H["asha"]).json()["ok"] is True
        victim = rows[len(rows) // 2 + 1]
        snap = dict(org_id=victim.org_id, user_id=victim.user_id, action=victim.action, detail=victim.detail,
                    created_at=victim.created_at, prev_hash=victim.prev_hash, row_hash=victim.row_hash, id=victim.id)
        db.delete(victim)
        db.commit()
        j = client.get("/audit/verify", headers=H["asha"]).json()
        assert j["ok"] is False and "removed" in j["reason"]
        db.execute(models.AuditLog.__table__.insert().values(**snap))
        db.commit()
        assert client.get("/audit/verify", headers=H["asha"]).json()["ok"] is True
    finally:
        db.close()


def test_csv_export(client, H):
    r = client.get("/audit/export.csv", params={"category": "Authentication"}, headers=H["asha"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,time_utc,category,actor") and len(lines) > 1
    assert all("Authentication" in ln for ln in lines[1:])
    assert client.get("/audit/export.csv", params={"category": "Nope"}, headers=H["asha"]).status_code == 400


def test_csv_formula_cells_are_neutralised():
    from app.auditchain import _safe
    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe("+1") == "'+1" and _safe("plain") == "plain"


def test_search_date_uses_the_viewers_timezone(client, H):
    from app.orgsearch import _date
    assert _date("2026-10-01", tz_offset=-330) == datetime.datetime(2026, 9, 30, 18, 30)       # India: starts 5.5h earlier in UTC
    assert _date("2026-10-01", end=True, tz_offset=-330) == datetime.datetime(2026, 10, 1, 18, 30)
    assert _date("2026-10-01") == datetime.datetime(2026, 10, 1)
    wide = client.get("/search", params={"date_from": "2000-01-01", "tz_offset": -330}, headers=H["asha"]).json()
    assert wide["total"] >= 1
    future = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    assert client.get("/search", params={"date_from": future, "tz_offset": -330}, headers=H["asha"]).json()["total"] == 0
    assert client.get("/search", params={"tz_offset": 99999}, headers=H["asha"]).status_code == 422
