"""Cross-department request by DESCRIPTION: requester never sees the other department's documents;
the approver picks the document. Runs late because earlier files change demo data."""
import pytest


def _docs(client, h):
    return client.get("/documents", headers=h).json()


def _mgr_doc(client, H):
    """A document in IT that meera (IT manager) can manage and kabir (HR) can't open."""
    r = client.post("/documents", headers=H["meera"], json={
        "title": "Vendor Contract Template zebra", "content": "Standard vendor contract template with payment terms.",
        "department": "IT", "visibility": "internal"})
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json().get("document", {}).get("id")


def test_full_flow(client, H, ID):
    did = _mgr_doc(client, H)
    assert did
    assert client.get(f"/documents/{did}", headers=H["kabir"]).status_code in (403, 404)
    r = client.post("/doc-requests", headers=H["kabir"], json={
        "department": "IT", "description": "vendor contract template", "reason": "legal review", "days": 7})
    assert r.status_code == 200 and r.json()["ok"]
    # requester sees only their own description, never titles
    mine = client.get("/doc-requests", headers=H["kabir"]).json()["mine"]
    assert mine[0]["description"] == "vendor contract template" and "documents" not in mine[0]
    rid = mine[0]["id"]
    # requester can neither list candidates nor decide
    assert client.get(f"/doc-requests/{rid}/candidates", headers=H["kabir"]).status_code == 404
    assert client.post(f"/doc-requests/{rid}/decide", headers=H["kabir"], json={"approve": True, "document_ids": [did]}).status_code == 404
    # an unrelated intern can't see or decide it either
    assert not client.get("/doc-requests", headers=H["priya"]).json()["for_review"]
    assert client.get(f"/doc-requests/{rid}/candidates", headers=H["priya"]).status_code == 404
    # IT manager sees it and the best match first
    rev = client.get("/doc-requests", headers=H["meera"]).json()["for_review"]
    assert any(x["id"] == rid for x in rev)
    cands = client.get(f"/doc-requests/{rid}/candidates", headers=H["meera"]).json()["documents"]
    assert cands and cands[0]["id"] == did and cands[0]["match"] > 0
    # approving needs a document
    assert client.post(f"/doc-requests/{rid}/decide", headers=H["meera"], json={"approve": True, "document_ids": []}).status_code == 400
    # a document from another department is refused
    other = [d for d in _docs(client, H["asha"]) if d.get("department") not in ("IT",)]
    if other:
        assert client.post(f"/doc-requests/{rid}/decide", headers=H["meera"], json={"approve": True, "document_ids": [other[0]["id"]]}).status_code == 400
    ok = client.post(f"/doc-requests/{rid}/decide", headers=H["meera"], json={"approve": True, "document_ids": [did], "days": 7})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.get(f"/documents/{did}", headers=H["kabir"]).status_code == 200
    mine = client.get("/doc-requests", headers=H["kabir"]).json()["mine"][0]
    assert mine["status"] == "approved" and mine["documents"][0]["id"] == did
    assert client.post(f"/doc-requests/{rid}/decide", headers=H["meera"], json={"approve": False}).status_code == 400


def test_validation_and_deny(client, H):
    assert client.post("/doc-requests", headers=H["kabir"], json={"department": "HR", "description": "anything", "days": 1}).status_code == 400
    assert client.post("/doc-requests", headers=H["kabir"], json={"department": "Nope", "description": "anything", "days": 1}).status_code == 400
    assert client.post("/doc-requests", headers=H["kabir"], json={"department": "IT", "description": "x", "days": 1}).status_code == 422
    assert client.post("/doc-requests", headers=H["kabir"], json={"department": "IT", "description": "salary sheet", "days": 3}).status_code == 400
    assert client.post("/doc-requests", headers=H["kabir"], json={"department": "IT", "description": "salary sheet", "days": 1}).status_code == 200
    rid = [x for x in client.get("/doc-requests", headers=H["kabir"]).json()["mine"] if x["description"] == "salary sheet"][0]["id"]
    assert client.post(f"/doc-requests/{rid}/decide", headers=H["meera"], json={"approve": False}).json()["status"] == "denied"
