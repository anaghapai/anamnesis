"""The Anamnesis demo story, step by step, through the real API. Tests in this file depend on each other (in order):
ask -> policy changes -> impact -> why -> revalidate -> history/rollback -> expert escalation -> human answer -> memory."""
import json

import pytest

S = {}                                   # state shared between the steps
VPN_OLD = "The VPN password rotates every 90 days and must be reset through the helpdesk portal."
VPN_NEW = "The VPN password rotates every 45 days and must be reset through the helpdesk portal. Contractors receive a separate VPN profile from the IT desk."
HUMAN_ANSWER = "The IT desk lead approves VPN access requests within two working days."


def docs_of(client, h):
    d = client.get("/documents", headers=h).json()
    return d if isinstance(d, list) else d.get("documents", [])


def text(r):
    return json.dumps(r.json()).lower()


# ------------------------------------------------------------ ask + grounding
def test_01_priya_asks_and_gets_a_sourced_answer(client, H):
    r = client.post("/ask", json={"question": "how often does the vpn password rotate"}, headers=H["priya"])
    assert r.status_code == 200
    j = r.json()
    S["qa"] = j["qa_id"]
    assert "90 days" in j["answer"]["text"]
    assert j["answer"]["document_title"].startswith("VPN Access Policy")
    assert j["state"]["code"] in ("stale", "current")           # the seeded VPN source is 120 days old -> flagged stale
    S["vpn"] = next(d["id"] for d in docs_of(client, H["meera"]) if d["title"].startswith("VPN"))


def test_02_no_evidence_means_no_answer(client, H):
    j = client.post("/ask", json={"question": "what is the airspeed velocity of an unladen swallow"}, headers=H["priya"]).json()
    assert not j.get("answered", False) or not (j.get("answer") or {}).get("text")


# ------------------------------------------------------------ change + impact
def test_03_only_editors_can_change_a_policy(client, H):
    r = client.put(f"/documents/{S['vpn']}/content", json={"content": VPN_NEW, "preview": False}, headers=H["priya"])
    assert r.status_code == 403
    r = client.put(f"/documents/{S['vpn']}/content", json={"content": VPN_NEW, "preview": False}, headers=H["kabir"])
    assert r.status_code in (403, 404)


def test_04_preview_shows_what_would_be_affected_and_changes_nothing(client, H):
    r = client.put(f"/documents/{S['vpn']}/content", json={"content": VPN_NEW, "preview": True}, headers=H["meera"])
    assert r.status_code == 200 and r.json()["applied"] is False
    a = r.json()["analysis"]
    assert {"from": "90", "to": "45"} in [c["delta"] for c in a["changes"]]
    assert S["qa"] in [x["qa_id"] for x in a["answers"]]
    cur = client.get(f"/documents/{S['vpn']}", headers=H["meera"]).json()
    assert "90 days" in json.dumps(cur)


def test_05_apply_creates_version_2_and_flags_dependent_answers(client, H):
    r = client.put(f"/documents/{S['vpn']}/content", json={"content": VPN_NEW, "preview": False}, headers=H["meera"])
    assert r.status_code == 200 and r.json()["applied"] is True and r.json()["version"] == 2
    v = client.get(f"/documents/{S['vpn']}/versions", headers=H["meera"]).json()
    assert [x["version"] for x in v["versions"]] == [1, 2] and v["current"] == 2


def test_06_compare_shows_the_change(client, H):
    r = client.get(f"/documents/{S['vpn']}/compare", params={"a": 1, "b": 2}, headers=H["meera"])
    assert r.status_code == 200
    assert {"from": "90", "to": "45"} in [c["delta"] for c in r.json()["changes"]]


def test_07_why_was_this_answer_flagged(client, H):
    r = client.get(f"/impact/qa/{S['qa']}/why", headers=H["priya"])
    assert r.status_code == 200 and r.json()["affected"] is True
    kinds = [s["kind"] for s in r.json()["steps"]]
    assert "source" in kinds and "change" in kinds
    assert any(s.get("delta") == {"from": "90", "to": "45"} for s in r.json()["steps"])


def test_08_other_people_cannot_read_priyas_explanation(client, H):
    assert client.get(f"/impact/qa/{S['qa']}/why", headers=H["kabir"]).status_code == 404
    assert client.get(f"/impact/qa/{S['qa']}/why", headers=H["rahul"]).status_code == 404


def test_09_affected_item_appears_in_the_managers_queue(client, H):
    q = client.get("/impact/overview", headers=H["meera"]).json()["queue"]
    assert S["qa"] in [x["qa_id"] for x in q]


def test_10_only_the_right_manager_can_revalidate(client, H):
    body = {"action": "replace", "new_answer": "The VPN password rotates every 45 days."}
    assert client.post(f"/impact/qa/{S['qa']}/revalidate", json=body, headers=H["kabir"]).status_code in (403, 404)
    assert client.post(f"/impact/qa/{S['qa']}/revalidate", json=body, headers=H["rahul"]).status_code == 404   # manager, wrong department
    r = client.post(f"/impact/qa/{S['qa']}/revalidate", json=body, headers=H["meera"])
    assert r.status_code == 200 and r.json()["status"] == "superseded"


def test_11_asking_again_returns_the_updated_answer(client, H):
    j = client.post("/ask", json={"question": "how often does the vpn password rotate"}, headers=H["priya"]).json()
    assert "45 days" in j["answer"]["text"] and "90 days" not in j["answer"]["text"]


# ---------------------------------------------------------------- history
def test_12_rollback_creates_a_new_version_and_keeps_history(client, H):
    r = client.post(f"/documents/{S['vpn']}/rollback", json={"version": 1}, headers=H["meera"])
    assert r.status_code == 200 and r.json()["applied"] is True and r.json()["version"] == 3
    v = client.get(f"/documents/{S['vpn']}/versions", headers=H["meera"]).json()
    assert [x["version"] for x in v["versions"]] == [1, 2, 3] and v["current"] == 3
    get = lambda n: client.get(f"/documents/{S['vpn']}/versions/{n}", headers=H["meera"]).json()["content"]
    assert get(3).strip() == get(1).strip()
    assert "45 days" in get(2)                                    # the middle version is still there


def test_13_version_history_respects_permissions(client, H):
    assert client.get(f"/documents/{S['vpn']}/versions", headers=H["kabir"]).status_code in (403, 404)
    assert client.post(f"/documents/{S['vpn']}/rollback", json={"version": 2}, headers=H["priya"]).status_code == 403


# ------------------------------------------------ permission-aware escalation
def test_14_no_experts_are_suggested_for_restricted_topics(client, H):
    r = client.post("/knowledge/experts", json={"question": "what is the CEO salary compensation"}, headers=H["priya"])
    assert r.status_code == 200 and r.json()["experts"] == []
    assert "ceo compensation review" not in text(r) and "42 lakh" not in text(r)


def test_15_experts_are_explained_and_authorized(client, H):
    j = client.post("/knowledge/experts", json={"question": "who approves vpn access"}, headers=H["priya"]).json()
    assert [e["name"] for e in j["experts"]] == ["Meera"]
    assert "authorized" in j["experts"][0]["why"] and "Suggested because" in j["experts"][0]["why"]


def test_16_cannot_route_a_question_to_an_ineligible_person(client, H, ID):
    r = client.post("/knowledge/requests", json={"question": "who approves vpn access", "assignee_id": ID["asha"]}, headers=H["priya"])
    assert r.status_code == 403


def test_17_create_request_then_only_assignee_can_answer(client, H):
    experts = client.post("/knowledge/experts", json={"question": "who approves vpn access"}, headers=H["priya"]).json()["experts"]
    r = client.post("/knowledge/requests", json={"question": "who approves vpn access", "assignee_id": experts[0]["id"]}, headers=H["priya"])
    assert r.status_code == 200, r.text
    S["req"] = r.json()["id"]
    body = {"answer": "I think it is me", "verify": True}
    assert client.post(f"/knowledge/requests/{S['req']}/answer", json=body, headers=H["kabir"]).status_code in (403, 404)
    assert client.post(f"/knowledge/requests/{S['req']}/answer", json=body, headers=H["priya"]).status_code in (403, 404)


def test_18_forwarding_rechecks_permissions(client, H, ID):
    opts = client.get(f"/knowledge/requests/{S['req']}/forward-options", headers=H["meera"]).json()["options"]
    names = [o["name"] for o in opts]
    assert "Kabir" not in names and "Priya" not in names
    r = client.post(f"/knowledge/requests/{S['req']}/forward", json={"to_user_id": ID["kabir"]}, headers=H["meera"])
    assert r.status_code == 403, "HR's Kabir is not authorized for the IT knowledge area, so forwarding must be refused"


def test_19_expert_can_decline(client, H):
    r = client.post("/knowledge/requests", json={"question": "how do I get a vpn token", "assignee_id": 3}, headers=H["priya"])
    assert r.status_code == 200, r.text
    d = client.post(f"/knowledge/requests/{r.json()['id']}/decline", json={"note": "not my area"}, headers=H["meera"])
    assert d.status_code == 200 and d.json()["status"] == "declined"


# -------------------------------------------------- human answer -> memory
def test_20_human_answer_is_verified_and_stored(client, H):
    r = client.post(f"/knowledge/requests/{S['req']}/answer", json={"answer": HUMAN_ANSWER, "verify": True}, headers=H["meera"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("verified", "stored", "closed", "done"), r.json()


def test_21_future_users_get_the_verified_answer(client, H):
    j = client.post("/ask", json={"question": "who approves vpn access"}, headers=H["priya"]).json()
    assert "IT desk lead" in json.dumps(j["answer"]) or "IT desk lead" in json.dumps(j)


def test_22_the_verified_answer_is_not_shown_outside_its_scope(client, H):
    j = client.post("/ask", json={"question": "who approves vpn access"}, headers=H["kabir"]).json()
    assert "IT desk lead" not in json.dumps(j)


# ---------------------------------------------------------------------- audit
def test_23_audit_log_records_the_story_and_is_manager_only(client, H):
    assert client.get("/audit-log", headers=H["kabir"]).status_code == 403
    assert client.get("/audit-log", headers=H["priya"]).status_code == 403
    r = client.get("/audit-log", headers=H["asha"])
    assert r.status_code == 200
    t = text(r)
    for action in ("document_rollback", "knowledge_declined", "impact_replaced"):
        assert action in t, action
