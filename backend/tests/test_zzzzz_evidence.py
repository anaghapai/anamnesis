"""Evidence Map: permission filtering, real-relationship edges, compare numbers, graph data. Runs last (data-changing)."""
import datetime
import json

import pytest


@pytest.fixture(scope="module")
def S():
    """Namespace of helpers that talk to the seeded temp database."""
    from app.db import SessionLocal
    from app import models

    class NS:
        pass

    ns = NS()
    ns.models = models
    ns.Session = SessionLocal

    def doc_id(title):
        db = SessionLocal()
        try:
            return db.query(models.Document).filter_by(title=title).first().id
        finally:
            db.close()

    def passage(did, title, text, dept="All", answer=False):
        p = {"chunk_id": 1, "text": text, "match": text, "before": "", "after": "", "heading": "", "score": 0.9,
             "document_id": did, "document_title": title, "department": dept, "age_days": 1}
        if answer:
            p["is_answer"] = True
            p["answer_text"] = text
        return p

    def make_qa(user_id, sources, question="evidence probe", answer="See the sources.", **kw):
        db = SessionLocal()
        try:
            u = db.get(models.User, user_id)
            qa = models.QARecord(org_id=u.org_id, user_id=user_id, department=u.department, question=question,
                                 answer_text=answer, sources=json.dumps(sources), **kw)
            db.add(qa)
            db.commit()
            return qa.id
        finally:
            db.close()

    ns.doc_id, ns.passage, ns.make_qa = doc_id, passage, make_qa
    return ns


def _assert_edges_backed(ev):
    ids = {c["id"] for c in ev["cards"]}
    assert len(ids) == len(ev["cards"]) <= 6
    for e in ev["edges"]:
        assert e["from"] in ids, e
        assert e["to"] == "answer" or e["to"] in ids, e


def _titles(ev):
    return [c["title"] for c in ev["cards"] if c["type"] == "document"]


# ------------------------------------------------------------ basic evidence ---

def test_real_vpn_answer_has_a_document_card_with_a_cites_line(client, H):
    r = client.post("/ask", headers=H["priya"], json={"question": "how often does the vpn password rotate"})
    assert r.status_code == 200
    ev = client.get(f"/ask/{r.json()['qa_id']}/evidence", headers=H["priya"]).json()
    doc = next(c for c in ev["cards"] if c["type"] == "document")
    assert doc["title"].startswith("VPN Access Policy") and doc["quote"]
    assert doc["status"] in ("stale", "current", "verified") and "days old" in doc["lines"][0]
    assert {"from": doc["id"], "to": "answer", "kind": "cites"} in ev["edges"]
    assert ev["state"] and ev["answer"]
    _assert_edges_backed(ev)


def test_an_old_unreviewed_document_is_marked_stale_with_its_age(client, H, ID, S):
    m = S.models
    db = S.Session()
    try:
        asha = db.get(m.User, ID["asha"])
        d = m.Document(org_id=asha.org_id, uploaded_by=asha.id, owner_id=asha.id, title="Evidence Stale Probe",
                       content="Old rule.", visibility="internal", department="All", workspace="company",
                       created_at=datetime.datetime.utcnow() - datetime.timedelta(days=150))
        db.add(d)
        db.commit()
        did = d.id
    finally:
        db.close()
    ev = client.get(f"/ask/{S.make_qa(ID['priya'], [S.passage(did, 'Evidence Stale Probe', 'Old rule.', answer=True)])}/evidence",
                    headers=H["priya"]).json()
    doc = next(c for c in ev["cards"] if c["type"] == "document")
    assert doc["status"] == "stale" and doc["status_label"] == "Stale"
    assert "150 days old" in doc["lines"][0] and "outdated" in doc["lines"][0]


def test_evidence_is_only_for_the_asker_and_needs_login(client, H, S):
    qa = S.make_qa(client.get("/me", headers=H["priya"]).json()["id"], [])
    assert client.get(f"/ask/{qa}/evidence", headers=H["kabir"]).status_code == 404
    assert client.get(f"/ask/{qa}/evidence", headers=H["priya"]).status_code == 200
    assert client.get(f"/ask/{qa}/evidence").status_code in (401, 403)
    assert client.get("/ask/999999/evidence", headers=H["priya"]).status_code == 404


# ---------------------------------------------------------------- permissions ---

HIDDEN_WORDS = ["CEO Compensation", "42 lakh", "Q3 Headcount", "hire two backend"]


def test_hidden_and_restricted_documents_never_appear(client, H, ID, S):
    ceo, hc, leave = S.doc_id("CEO Compensation Review"), S.doc_id("Q3 Headcount Plan"), S.doc_id("Leave Policy (HR)")
    sources = [
        S.passage(ceo, "CEO Compensation Review", "The CEO's current annual salary is 42 lakh INR.", answer=True),
        S.passage(hc, "Q3 Headcount Plan", "Engineering plans to hire two backend engineers."),
        S.passage(leave, "Leave Policy (HR)", "Employees receive 18 days of paid leave per year.", dept="HR"),
    ]
    # kabir: HR member -> restricted (not on the list) and confidential (not a manager) must both be invisible
    qa = S.make_qa(ID["kabir"], sources)
    raw = client.get(f"/ask/{qa}/evidence", headers=H["kabir"]).text
    ev = json.loads(raw)
    assert _titles(ev) == ["Leave Policy (HR)"]
    for w in HIDDEN_WORDS:
        assert w not in raw, f"leaked: {w}"
    _assert_edges_backed(ev)
    # meera: IT manager -> confidential yes, restricted still no
    qa = S.make_qa(ID["meera"], sources)
    raw = client.get(f"/ask/{qa}/evidence", headers=H["meera"]).text
    assert "CEO Compensation" not in raw and "42 lakh" not in raw
    assert "Q3 Headcount Plan" in _titles(json.loads(raw))
    # rahul is on the restricted document's allow-list, asha is the owner: both may see it
    for who in ("rahul", "asha"):
        qa = S.make_qa(ID[who], sources)
        ev = client.get(f"/ask/{qa}/evidence", headers=H[who]).json()
        assert "CEO Compensation Review" in _titles(ev)


def test_recycle_bin_and_personal_documents_never_appear(client, H, ID, S):
    m = S.models
    db = S.Session()
    try:
        asha = db.get(m.User, ID["asha"])
        gone = m.Document(org_id=asha.org_id, uploaded_by=asha.id, owner_id=asha.id, title="Evidence Bin Probe",
                          content="Binned text.", visibility="internal", department="All", workspace="company",
                          deleted_at=datetime.datetime.utcnow())
        mine = m.Document(org_id=asha.org_id, uploaded_by=asha.id, owner_id=asha.id, title="Evidence Personal Probe",
                          content="Private text.", visibility="internal", department="All", workspace="personal")
        db.add_all([gone, mine])
        db.commit()
        gid, pid = gone.id, mine.id
    finally:
        db.close()
    sources = [S.passage(gid, "Evidence Bin Probe", "Binned text."), S.passage(pid, "Evidence Personal Probe", "Private text.")]
    raw = client.get(f"/ask/{S.make_qa(ID['kabir'], sources)}/evidence", headers=H["kabir"]).text
    assert "Evidence Bin Probe" not in raw and "Evidence Personal Probe" not in raw
    raw = client.get(f"/ask/{S.make_qa(ID['asha'], sources)}/evidence", headers=H["asha"]).text
    assert "Evidence Bin Probe" not in raw                      # in the bin = gone for everybody
    assert "Evidence Personal Probe" in raw                     # the author may see their own private document


def test_an_expired_grant_removes_the_card(client, H, ID, S):
    m = S.models
    ceo = S.doc_id("CEO Compensation Review")
    src = [S.passage(ceo, "CEO Compensation Review", "The CEO's current annual salary is 42 lakh INR.", answer=True)]
    db = S.Session()
    try:
        org = db.get(m.User, ID["kabir"]).org_id
        g = m.DocumentGrant(org_id=org, document_id=ceo, user_id=ID["kabir"], granted_by=ID["asha"], source="direct",
                            expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=1))
        db.add(g)
        db.commit()
        gid = g.id
    finally:
        db.close()
    qa = S.make_qa(ID["kabir"], src)
    assert "CEO Compensation Review" in _titles(client.get(f"/ask/{qa}/evidence", headers=H["kabir"]).json())
    db = S.Session()
    try:
        db.get(m.DocumentGrant, gid).expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    raw = client.get(f"/ask/{qa}/evidence", headers=H["kabir"]).text
    assert "CEO Compensation" not in raw and "42 lakh" not in raw


def test_fact_card_never_names_a_document_the_user_cannot_open(client, H, ID, S):
    m = S.models
    ceo = S.doc_id("CEO Compensation Review")
    db = S.Session()
    try:
        asha = db.get(m.User, ID["asha"])
        f = m.Fact(org_id=asha.org_id, document_id=ceo, subject="Evidence Probe Topic", relation="relates_to",
                   object="Evidence Probe Object", status="active", created_by=asha.id)
        db.add(f)
        db.commit()
        fid = f.id
    finally:
        db.close()
    hop = {"kind": "hop", "path": "Evidence Probe Topic", "hops": [{"fact_id": fid, "subject": "Evidence Probe Topic"}]}
    ceo_p = S.passage(ceo, "CEO Compensation Review", "The CEO's current annual salary is 42 lakh INR.")
    # kabir: the fact (org-scoped by design) may show, its restricted source document may not
    qa = S.make_qa(ID["kabir"], [ceo_p, hop])
    raw = client.get(f"/ask/{qa}/evidence", headers=H["kabir"]).text
    ev = json.loads(raw)
    assert any(c["type"] == "fact" and "Evidence Probe Topic" in c["title"] for c in ev["cards"])
    assert "CEO Compensation" not in raw and "42 lakh" not in raw
    assert not any(e["kind"] == "states" for e in ev["edges"])
    _assert_edges_backed(ev)
    g = client.get("/evidence/graph", headers=H["kabir"])
    edge = next(e for e in g.json()["edges"] if e["id"] == fid)
    assert edge["doc"] is None and edge["department"] is None
    assert "CEO Compensation" not in g.text
    # asha may open it, so for her the source document is attached
    edge = next(e for e in client.get("/evidence/graph", headers=H["asha"]).json()["edges"] if e["id"] == fid)
    assert edge["doc"]["title"] == "CEO Compensation Review"
    ev = client.get(f"/ask/{S.make_qa(ID['asha'], [ceo_p, hop])}/evidence", headers=H["asha"]).json()
    assert any(e["kind"] == "states" for e in ev["edges"])


# ------------------------------------------------------------ other card types ---

def test_conflict_card_shows_both_versions_and_real_lines(client, H):
    r = client.post("/ask", headers=H["rahul"], json={"question": "when is the postgres migration deadline"}).json()
    ev = client.get(f"/ask/{r['qa_id']}/evidence", headers=H["rahul"]).json()
    con = next(c for c in ev["cards"] if c["type"] == "conflict")
    assert [v["text"] for v in con["versions"]] == ["September 25", "October 2"]
    assert con["status"] == "conflict" and "manager" in con["lines"][0]
    assert any(e["to"] == con["id"] and e["kind"] == "conflicts" for e in ev["edges"])
    assert ev["state"] and ev["state"]["code"]          # the existing badge is passed through unchanged
    _assert_edges_backed(ev)


def test_verified_card_names_reviewer_role_and_links_to_answer(client, H, ID, S):
    m = S.models
    q = "how many standing desks does finance order each year"
    db = S.Session()
    try:
        meera = db.get(m.User, ID["meera"])
        db.add(m.QARecord(org_id=meera.org_id, user_id=ID["priya"], department="IT", question=q,
                          answer_text="Finance orders twelve standing desks a year.", sources="[]",
                          status="verified", reviewer_id=meera.id, reviewed_at=datetime.datetime.utcnow(),
                          review_note="Checked with finance"))
        db.commit()
    finally:
        db.close()
    r = client.post("/ask", headers=H["priya"], json={"question": q}).json()
    assert r["verified"], "the verified answer should be served first"
    ev = client.get(f"/ask/{r['qa_id']}/evidence", headers=H["priya"]).json()
    ver = next(c for c in ev["cards"] if c["type"] == "verified")
    assert ver["lines"][0] == "Reviewed by IT manager" and "Meera" in ver["lines"]
    assert ver["status"] == "verified" and ver["date"]
    assert {"from": ver["id"], "to": "answer", "kind": "reviewed"} in ev["edges"]
    assert ev["answer"].startswith("Finance orders")
    _assert_edges_backed(ev)


def test_flagged_answer_shows_what_was_flagged_and_who_decides(client, H, ID, S):
    qa = S.make_qa(ID["priya"], [], status="flagged", flag_note="Rotation is 60 days now",
                   flagged_at=datetime.datetime.utcnow())
    ev = client.get(f"/ask/{qa}/evidence", headers=H["priya"]).json()
    rev = next(c for c in ev["cards"] if c["type"] == "review")
    assert "Rotation is 60 days now" in " ".join(rev["lines"]) and rev["status"] == "review"
    assert {"from": rev["id"], "to": "answer", "kind": "reviewed"} in ev["edges"]


def test_used_for_card_is_a_count_without_names(client, H, ID, S):
    m = S.models
    qa = S.make_qa(ID["priya"], [])
    db = S.Session()
    try:
        org = db.get(m.User, ID["priya"]).org_id
        for uid in (ID["priya"], ID["kabir"]):
            db.add(m.UsageEvent(org_id=org, user_id=uid, qa_id=qa, note="writing the onboarding guide"))
        db.commit()
    finally:
        db.close()
    raw = client.get(f"/ask/{qa}/evidence", headers=H["priya"]).text
    used = next(c for c in json.loads(raw)["cards"] if c["type"] == "used")
    assert used["count"] == 2 and "2 people" in used["lines"][0]
    for banned in ("Priya", "Kabir", "priya", "kabir", "onboarding guide"):
        assert banned not in json.dumps(used)


def test_never_more_than_six_cards(client, H, ID, S):
    m = S.models
    db = S.Session()
    try:
        asha = db.get(m.User, ID["asha"])
        docs = []
        for i in range(9):
            d = m.Document(org_id=asha.org_id, uploaded_by=asha.id, owner_id=asha.id, title=f"Evidence Many {i}",
                           content="Many.", visibility="public", department="All", workspace="company")
            db.add(d); docs.append(d)
        db.commit()
        ids = [(d.id, d.title) for d in docs]
    finally:
        db.close()
    ev = client.get(f"/ask/{S.make_qa(ID['kabir'], [S.passage(i, t, 'Many.') for i, t in ids])}/evidence", headers=H["kabir"]).json()
    assert len(ev["cards"]) == 6
    _assert_edges_backed(ev)


# --------------------------------------------------------- search vs Anamnesis ---

def test_compare_counts_only_what_the_user_may_open_and_stores_nothing(client, H, ID, S):
    m = S.models
    q = "ceo compensation salary"
    kabir = client.get("/evidence/compare", params={"q": q}, headers=H["kabir"])
    assert kabir.status_code == 200
    for w in HIDDEN_WORDS:
        assert w not in kabir.text
    rows = {r["label"]: r["count"] for r in kabir.json()["plain"]["rows"]}
    assert rows["Documents"] == 0 and rows["Passages"] == 0
    asha = client.get("/evidence/compare", params={"q": q}, headers=H["asha"]).json()
    assert {r["label"]: r["count"] for r in asha["plain"]["rows"]}["Documents"] >= 1
    assert asha["plain"]["total"] == sum(r["count"] for r in asha["plain"]["rows"])
    assert asha["plain"]["missing"] == ["No context", "No relationships", "No proof"]

    db = S.Session()
    try:
        before = (db.query(m.QARecord).count(), db.query(m.AuditLog).count())
    finally:
        db.close()
    r = client.get("/evidence/compare", params={"q": "how often does the vpn password rotate"}, headers=H["priya"]).json()
    assert r["anamnesis"]["cards"] and r["anamnesis"]["answer"]
    _assert_edges_backed(r["anamnesis"])
    db = S.Session()
    try:
        after = (db.query(m.QARecord).count(), db.query(m.AuditLog).count())
    finally:
        db.close()
    assert before == after, "compare must be a dry run"
    assert client.get("/evidence/compare", params={"q": "vpn"}).status_code in (401, 403)


# ------------------------------------------------------------------ graph data ---

def test_graph_endpoint_has_nodes_status_and_openable_sources_only(client, H):
    g = client.get("/evidence/graph", headers=H["priya"]).json()
    labels = {n["label"] for n in g["nodes"]}
    assert {"Postgres Migration", "Rahul"} <= labels
    assert any(e["status"] == "conflicting" for e in g["edges"])
    assert next(n for n in g["nodes"] if n["label"] == "Postgres Migration")["conflict"] is True
    for e in g["edges"]:
        assert set(e) >= {"id", "from", "to", "label", "status", "department", "added_by", "doc"}
    assert client.get("/evidence/graph").status_code in (401, 403)
