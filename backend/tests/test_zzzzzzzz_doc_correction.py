"""Reviewer corrects an answer AND the source document: new version, history kept, owner approval
when the reviewer isn't the owner."""


def _ask(client, h, q):
    r = client.post("/ask", headers=h, json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


def _flag_and_get(client, H, qid, note="Policy changed"):
    assert client.post(f"/qa/{qid}/flag", headers=H["priya"], json={"note": note}).status_code == 200
    rows = client.get("/reviews", headers=H["meera"]).json()
    return [r for r in rows if r["id"] == qid][0]


def _qa_id(client, H, ans):
    return ans["qa_id"]


def test_owner_correction_updates_document_and_keeps_versions(client, H, ID):
    ans = _ask(client, H["priya"], "how often does the vpn password rotate")
    qid = _qa_id(client, H, ans)
    rev = _flag_and_get(client, H, qid)
    src = [s for s in rev["sources"] if s.get("document_id") and "90 days" in (s.get("text") or "")][0]
    # wrong document / unknown wording are refused and change nothing
    bad = client.post(f"/reviews/{qid}", headers=H["meera"], json={
        "verdict": "correct", "corrected_answer": "45 days",
        "doc_edit": {"document_id": src["document_id"], "old_text": "text that is not there", "new_text": "x"}})
    assert bad.status_code == 400
    assert client.get("/reviews", headers=H["meera"]).json()  # still waiting
    before = client.get(f"/documents/{src['document_id']}/versions", headers=H["priya"]).json()["current"]
    old = "The VPN password rotates every 90 days"
    ok = client.post(f"/reviews/{qid}", headers=H["meera"], json={
        "verdict": "correct", "corrected_answer": "Every 45 days.",
        "doc_edit": {"document_id": src["document_id"], "old_text": old,
                     "new_text": "The VPN password rotates every 45 days"}})
    assert ok.status_code == 200, ok.text
    d = ok.json()["document"]
    assert d["applied"] and d["version"] == before + 1
    doc = client.get(f"/documents/{src['document_id']}", headers=H["priya"]).json()
    assert "every 45 days" in doc["content"] and "90 days" not in doc["content"]
    vs = client.get(f"/documents/{src['document_id']}/versions", headers=H["priya"]).json()
    assert vs["current"] == before + 1 and [v["version"] for v in vs["versions"]][0] == 1
    v1 = client.get(f"/documents/{src['document_id']}/versions/1", headers=H["priya"]).json()
    assert "90 days" in v1["content"]
    # asking again: only the corrected wording comes back
    again = _ask(client, H["priya"], "how often does the vpn password rotate")
    blob = str(again)
    assert "45 days" in blob and "90 days" not in blob
    # the corrected answer is not flagged as outdated itself
    assert again["qa_id"] != qid


def test_non_owner_needs_owner_approval(client, H, ID):
    # a document owned by asha (admin) in IT; meera (IT manager, not the owner) proposes a fix
    r = client.post("/documents", headers=H["asha"], json={
        "title": "Laptop Refresh Policy zebra", "content": "Laptops are refreshed every 3 years for all staff.",
        "department": "IT", "visibility": "internal"})
    assert r.status_code in (200, 201), r.text
    did = r.json().get("id") or r.json().get("document", {}).get("id")
    ans = _ask(client, H["priya"], "how often are laptops refreshed")
    qid = ans["qa_id"]
    assert client.post(f"/qa/{qid}/flag", headers=H["priya"], json={"note": "old"}).status_code == 200
    rev = [x for x in client.get("/reviews", headers=H["meera"]).json() if x["id"] == qid][0]
    assert any(s.get("document_id") == did for s in rev["sources"])
    res = client.post(f"/reviews/{qid}", headers=H["meera"], json={
        "verdict": "correct", "corrected_answer": "Every 4 years.",
        "doc_edit": {"document_id": did, "old_text": "every 3 years", "new_text": "every 4 years"}})
    assert res.status_code == 200, res.text
    assert res.json()["document"]["pending"] is True
    # document is NOT changed yet
    assert "3 years" in client.get(f"/documents/{did}", headers=H["asha"]).json()["content"]
    pend = [x for x in client.get("/dual-control", headers=H["asha"]).json()
            if x["document_id"] == did and x["status"] == "pending"][0]
    assert pend["detail"]["new"] == "every 4 years" and pend["can_decide"]
    # the requester can't approve their own request; a different manager who isn't the owner can't either
    assert client.post(f"/dual-control/{pend['id']}/decide", headers=H["meera"], json={"approve": True}).status_code == 403
    assert client.post(f"/dual-control/{pend['id']}/decide", headers=H["rahul"], json={"approve": True}).status_code == 403
    ok = client.post(f"/dual-control/{pend['id']}/decide", headers=H["asha"], json={"approve": True})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert "every 4 years" in client.get(f"/documents/{did}", headers=H["asha"]).json()["content"]
    vs = client.get(f"/documents/{did}/versions", headers=H["asha"]).json()
    assert [v["version"] for v in vs["versions"]] == [1, 2]
