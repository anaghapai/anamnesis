"""Audit log viewer tests (runs after the demo story so there is real history to look at)."""
import datetime


def events(client, h, **params):
    r = client.get("/audit/events", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_managers_only(client, H):
    for who in ("priya", "kabir"):
        assert client.get("/audit/events", headers=H[who]).status_code == 403
        assert client.get("/audit/events/1", headers=H[who]).status_code == 403
    for who in ("meera", "asha"):
        assert client.get("/audit/events", headers=H[who]).status_code == 200


def test_no_login_no_audit(client):
    assert client.get("/audit/events").status_code in (401, 403)


def test_categories_reflect_the_demo_story(client, H):
    cats = {c["name"]: c["count"] for c in events(client, H["asha"])["categories"]}
    for name in ("Authentication", "Answer generation", "Verification", "Escalation", "Rollbacks"):
        assert cats.get(name, 0) > 0, (name, cats)


def test_category_filter_returns_only_that_category(client, H):
    j = events(client, H["asha"], category="Rollbacks")
    assert j["total"] >= 1 and all(e["category"] == "Rollbacks" for e in j["events"])
    assert all(e["action"] == "document_rollback" for e in j["events"])


def test_events_are_readable_and_complete(client, H):
    e = events(client, H["asha"], category="Rollbacks")["events"][0]
    assert "rolled a document back" in e["summary"] and e["actor"] == "Meera" and e["created_at"]


def test_search_actor_and_dates(client, H, ID):
    assert events(client, H["asha"], q="rollback")["total"] >= 1
    assert events(client, H["asha"], q="meera")["total"] >= 1                       # matches the actor's name
    by = events(client, H["asha"], actor_id=ID["meera"])
    assert by["total"] >= 1 and all(e["actor_id"] == ID["meera"] for e in by["events"])
    tomorrow = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    assert events(client, H["asha"], date_from=tomorrow)["total"] == 0
    now = datetime.datetime.utcnow()
    today_utc = now.date().isoformat()
    assert events(client, H["asha"], date_from=today_utc, date_to=today_utc)["total"] >= 1           # UTC day
    # the viewer's own calendar day: in India (UTC+5:30) it can already be tomorrow there
    ist_today = (now + datetime.timedelta(minutes=330)).date().isoformat()
    assert events(client, H["asha"], date_from=ist_today, date_to=ist_today, tz_offset=-330)["total"] >= 1
    # and a day that has not started yet for that viewer shows nothing
    ist_next = (now + datetime.timedelta(minutes=330, days=1)).date().isoformat()
    assert events(client, H["asha"], date_from=ist_next, tz_offset=-330)["total"] == 0


def test_bad_input_is_rejected(client, H):
    assert client.get("/audit/events", params={"date_from": "yesterday"}, headers=H["asha"]).status_code == 400
    assert client.get("/audit/events", params={"category": "Nonsense"}, headers=H["asha"]).status_code == 400


def test_paging(client, H):
    j = events(client, H["asha"], limit=5)
    assert len(j["events"]) == 5 and j["total"] > 5
    j2 = events(client, H["asha"], limit=5, offset=5)
    assert {e["id"] for e in j["events"]}.isdisjoint({e["id"] for e in j2["events"]})


def test_detail_view(client, H):
    eid = events(client, H["asha"], category="Rollbacks")["events"][0]["id"]
    r = client.get(f"/audit/events/{eid}", headers=H["asha"])
    assert r.status_code == 200
    j = r.json()
    assert j["raw"]["action"] == "document_rollback" and j["event"]["actor"] == "Meera"
    assert isinstance(j["same_actor_within_5_minutes"], list)
    assert client.get("/audit/events/99999999", headers=H["asha"]).status_code == 404


def test_log_is_append_only_through_the_api_and_described_honestly(client, H):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/audit/events/1", headers=H["asha"]).status_code in (404, 405)
    note = events(client, H["asha"])["note"].lower()
    assert "append-only" in note and "hash-chained" in note


def test_original_audit_endpoint_still_works(client, H):
    r = client.get("/audit-log", headers=H["asha"])
    assert r.status_code == 200 and len(r.json()) > 5
