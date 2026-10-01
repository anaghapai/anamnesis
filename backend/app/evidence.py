"""Evidence Map API.

  GET /ask/{qa_id}/evidence   cards + real relationships behind one of the asker's own answers
  GET /evidence/compare?q=    "Plain search vs Anamnesis" numbers and a dry-run evidence map (stores nothing)
  GET /evidence/graph         knowledge-graph data for the Graph screen (documents shown only when openable)

Rules this module keeps:
  * A document becomes a card, a link, a title or a count ONLY if retrieval.user_can_see_document() says the
    signed-in user may open it (the same gate search uses). Hidden and restricted documents never appear,
    not even as a count or a hint. The only "hidden source" hint is the existing Request access box, which the
    frontend already receives from /ask and which this module does not touch.
  * Lines (edges) are returned only when stored data backs them: document -> answer (cited), document -> fact
    (fact.document_id or a source link), fact -> answer (multi-hop chain), fact -> conflict (open conflict row),
    review -> answer (verified/flagged/impact flag), used -> answer (usage events).
  * Facts stay org-scoped exactly as graph.visible_facts() defines them; only their document details are gated.
  * Nothing here writes to the database.
"""
import datetime
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from . import models, auth, retrieval, graph, impact, nlp, features
from .db import get_db

router = APIRouter()

MAX_CARDS = 6                      # 2 x 3 grid
TYPE_ORDER = ["document", "verified", "review", "fact", "conflict", "used"]      # layout order (row by row)
KEEP_PRIORITY = ["conflict", "review", "verified", "used", "document", "fact"]  # what survives when > 6 exist


# ------------------------------------------------------------------ helpers ---

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _clip(text, n=240) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "\u2026"


class _Docs:
    """Per-request cache of 'may this user open document X?'. None means: do not show anything about it."""

    def __init__(self, db: Session, user: models.User):
        self.db, self.user, self._c = db, user, {}

    def get(self, doc_id) -> Optional[models.Document]:
        if doc_id is None:
            return None
        if doc_id not in self._c:
            d = self.db.get(models.Document, doc_id)
            ok = bool(d and d.deleted_at is None and retrieval.user_can_see_document(self.user, d, self.db))
            self._c[doc_id] = d if ok else None
        return self._c[doc_id]


def _fact_doc_ids(db: Session, fact: models.Fact):
    ids = [fact.document_id] if fact.document_id else []
    for l in db.query(models.FactDocLink).filter_by(fact_id=fact.id, kind="source").all():
        if l.document_id not in ids:
            ids.append(l.document_id)
    return ids


def _fact_doc(db: Session, docs: _Docs, fact: models.Fact) -> Optional[models.Document]:
    """The first source document of a fact that this user may open (or None)."""
    for did in _fact_doc_ids(db, fact):
        d = docs.get(did)
        if d is not None:
            return d
    return None


def _user_name(db: Session, uid, cache) -> Optional[str]:
    if not uid:
        return None
    if uid not in cache:
        u = db.get(models.User, uid)
        cache[uid] = u.name if u else None
    return cache[uid]


def _doc_is_stale(doc: models.Document, age_days: int) -> bool:
    fresh = bool(doc.verified_until and doc.verified_until >= datetime.datetime.utcnow())
    return age_days > impact.STALE_DAYS and not fresh


def _age_days(doc: models.Document) -> int:
    base = max(doc.created_at, doc.last_reviewed_at or doc.created_at)
    return (datetime.datetime.utcnow() - base).days


# -------------------------------------------------------------- card builders ---

def _document_cards(db, docs: _Docs, m: dict):
    out = []
    answer_doc = (m.get("answer") or {}).get("document_id")
    groups = list(m.get("documents") or [])
    groups.sort(key=lambda g: 0 if g.get("document_id") == answer_doc else 1)
    for g in groups:
        d = docs.get(g.get("document_id"))
        if d is None:
            continue                                  # not openable by this user -> no card at all
        age = _age_days(d)
        stale = _doc_is_stale(d, age)
        verified = bool(d.verified_until and d.verified_until >= datetime.datetime.utcnow())
        passages = g.get("passages") or []
        top = next((p for p in passages if p.get("is_answer")), passages[0] if passages else {})
        quote = _clip(top.get("match") or top.get("text"), 220)
        status, label = ("stale", "Stale") if stale else (("verified", "Verified") if verified else ("current", "Current"))
        out.append({
            "id": f"doc-{d.id}", "type": "document", "title": d.title,
            "lines": [f"{d.department or 'All'} \u00b7 {(d.visibility or 'internal').capitalize()} \u00b7 {age} days old"
                      + (" \u00b7 may be outdated" if stale else "")],
            "quote": quote, "status": status, "status_label": label,
            "date": _iso(max(d.created_at, d.last_reviewed_at or d.created_at)),
            "link": {"kind": "document", "id": d.id, "highlight": top.get("match") or top.get("text")},
            "is_answer_doc": d.id == answer_doc,
        })
    return out


def _fact_card(db, docs, f: models.Fact, names) -> dict:
    d = _fact_doc(db, docs, f)
    who = _user_name(db, f.created_by, names)
    bits = [f"Added by {who}" if who else "Added by the system"]
    if d is not None:
        bits.append(f"from {d.title}")
    conflict = f.status == "conflicting"
    return {
        "id": f"fact-{f.id}", "type": "fact", "title": f"{f.subject} {f.relation} {f.object}",
        "lines": [" \u00b7 ".join(bits)],
        "status": "conflict" if conflict else "current", "status_label": "Conflict" if conflict else "Current",
        "date": _iso(f.created_at), "link": {"kind": "node", "label": f.subject},
        "fact_id": f.id, "doc_id": d.id if d is not None else None,
    }


def _conflict_card(db, f: models.Fact, names) -> Optional[dict]:
    c = db.query(models.Conflict).filter(
        models.Conflict.org_id == f.org_id, models.Conflict.resolved == False,        # noqa: E712
        (models.Conflict.old_fact_id == f.id) | (models.Conflict.new_fact_id == f.id)).first()
    if not c:
        return None
    old, new = db.get(models.Fact, c.old_fact_id), db.get(models.Fact, c.new_fact_id)
    if not old or not new:
        return None
    waiting = "Waiting for a manager"
    if c.proposed_by:
        who = _user_name(db, c.proposed_by, names)
        waiting = f"Proposed by {who}, waiting for a manager" if who else waiting
    return {
        "id": f"conflict-{c.id}", "type": "conflict", "title": f"{old.subject} {old.relation}",
        "lines": [waiting], "versions": [{"label": "Older", "text": old.object}, {"label": "Newer", "text": new.object}],
        "status": "conflict", "status_label": "Conflict", "date": _iso(c.created_at),
        "link": {"kind": "view", "view": "conflicts"}, "conflict_id": c.id,
        "fact_ids": [old.id, new.id],
    }


def _reviewer_title(db, rid, names) -> Optional[str]:
    u = db.get(models.User, rid) if rid else None
    if not u:
        return None
    names[("title", rid)] = f"{u.department} {u.role.lower()}" if u.department else u.role.lower()
    return u.name


def _verified_card(db, user, m: dict, names) -> Optional[dict]:
    v = m.get("verified")
    if not v:
        return None
    rec = db.get(models.QARecord, v.get("qa_id"))
    if not rec or rec.org_id != user.org_id:
        return None
    # same rule find_verified() applies before serving it: department gate + no private-workspace sources
    if rec.user_id != user.id and (not retrieval.dept_ok(user, rec.department, db) or retrieval._cites_private_doc(db, rec)):
        return None
    name = _reviewer_title(db, rec.reviewer_id, names)
    role = names.get(("title", rec.reviewer_id))
    lines = [f"Reviewed by {role}" if role else "Reviewed by a supervisor"]
    if name:
        lines.append(name)
    chip = ("review", "Needs review") if v.get("needs_rereview") else ("verified", "Verified")
    return {
        "id": f"ver-{rec.id}", "type": "verified", "lines": lines,
        "title": "Corrected answer" if v.get("kind") == "corrected" else "Verified answer",
        "quote": _clip(v.get("answer"), 200), "status": chip[0], "status_label": chip[1],
        "date": _iso(rec.reviewed_at), "link": None,
    }


def _review_cards(db, user, qa: Optional[models.QARecord], names):
    """Flag / correction on this very answer, and the 'a source changed' flag from Knowledge Impact."""
    out = []
    if qa is None or qa.user_id != user.id:
        return out
    if qa.status in ("flagged", "corrected", "verified", "rejected") and not qa.served_from_qa_id:
        lines = []
        if qa.flag_note:
            lines.append(f"Flagged: {_clip(qa.flag_note, 120)}")
        elif qa.status == "flagged":
            lines.append("Flagged as wrong or outdated")
        if qa.corrected_answer:
            lines.append(f"Correction: {_clip(qa.corrected_answer, 120)}")
        who = _user_name(db, qa.reviewer_id, names)
        if qa.status == "flagged":
            lines.append("Waiting for a reviewer")
        elif who:
            lines.append(f"Decided by {who} ({qa.status})")
        ok = qa.status in ("verified", "corrected")
        out.append({
            "id": f"rev-{qa.id}", "type": "review", "title": "Review" if qa.status != "flagged" else "Flagged answer",
            "lines": lines or [qa.status.capitalize()],
            "status": "verified" if ok else "review", "status_label": "Verified" if ok else ("Needs review" if qa.status == "flagged" else qa.status.capitalize()),
            "date": _iso(qa.reviewed_at or qa.flagged_at), "link": None,
        })
    flag = impact.flag_for(db, qa)
    if flag:
        out.append({
            "id": f"impact-{qa.id}", "type": "review", "title": "A source changed",
            "lines": [_clip(flag.get("reason") or "Wording this answer relied on was edited.", 140)],
            "status": "review", "status_label": "Needs review", "date": None, "link": None,
        })
    return out


def _used_card(db, user, qa: Optional[models.QARecord]) -> Optional[dict]:
    """Count only. Never names, never which document."""
    if qa is None:
        return None
    ids = {qa.id}
    root = qa.served_from_qa_id or qa.id
    ids.add(root)
    ids |= {r[0] for r in db.query(models.QARecord.id).filter(
        models.QARecord.org_id == user.org_id, models.QARecord.served_from_qa_id == root)}
    rows = db.query(models.UsageEvent).filter(
        models.UsageEvent.org_id == user.org_id, models.UsageEvent.qa_id.in_(ids)).all()
    people = {e.user_id for e in rows}
    if not people:
        return None
    n = len(people)
    last = max((e.created_at for e in rows if e.created_at), default=None)
    return {
        "id": "used", "type": "used", "title": "Used for work",
        "lines": [f"{n} {'person' if n == 1 else 'people'} marked \u201cI used this\u201d"],
        "status": "current", "status_label": "In use", "date": _iso(last), "link": None, "count": n,
    }


# ------------------------------------------------------------------ assembly ---

def build_evidence(db: Session, user: models.User, m: dict, qa: Optional[models.QARecord] = None) -> dict:
    """m is the same dict /chats returns for one answer (or a dry-run of it)."""
    docs, names = _Docs(db, user), {}
    cards = _document_cards(db, docs, m)
    shown_doc_ids = {c["link"]["id"] for c in cards}

    # facts: the multi-hop chain, plus conflicting facts that come from a document already shown
    hop_ids = [h.get("fact_id") for h in ((m.get("multi_hop") or {}).get("hops") or []) if h.get("fact_id")]
    fact_rows, seen = [], set()
    for fid in hop_ids:
        f = db.get(models.Fact, fid)
        if f and f.org_id == user.org_id and f.status != "superseded" and f.id not in seen:
            fact_rows.append((f, True)); seen.add(f.id)
    if shown_doc_ids:
        # a conflicting fact from a document already shown, but only when it concerns something this answer is about
        # (an entity in the multi-hop chain, or a word of the question) - so unrelated conflicts never clutter the map
        about = {str(x).strip().lower() for f, _ in fact_rows for x in (f.subject, f.object)}
        qtoks = _tokens(m.get("question") or "")
        extra = db.query(models.Fact).filter(
            models.Fact.org_id == user.org_id, models.Fact.status == "conflicting",
            models.Fact.document_id.in_(shown_doc_ids)).all()
        for f in extra:
            labels = (f.subject.strip().lower(), f.object.strip().lower())
            related = any(x in about for x in labels) or any(t in lab for t in qtoks for lab in labels)
            if f.id not in seen and related:
                fact_rows.append((f, False)); seen.add(f.id)

    fact_cards, conflict_cards, edges = [], [], []
    for f, in_chain in fact_rows:
        fc = _fact_card(db, docs, f, names)
        fc["in_chain"] = in_chain
        fact_cards.append(fc)
        if f.status == "conflicting":
            cc = _conflict_card(db, f, names)
            if cc and all(cc["id"] != x["id"] for x in conflict_cards):
                conflict_cards.append(cc)

    ver = _verified_card(db, user, m, names)
    reviews = _review_cards(db, user, qa, names)
    used = _used_card(db, user, qa)

    fact_cards.sort(key=lambda c: 0 if c["status"] == "conflict" else 1)       # conflicting facts keep their conflict card linked
    pool = cards + ([ver] if ver else []) + reviews + fact_cards + conflict_cards + ([used] if used else [])
    keep = []
    for t in KEEP_PRIORITY:                       # when more than 6 exist, keep the most telling types first
        keep += [c for c in pool if c["type"] == t]
    keep = keep[:MAX_CARDS]
    ids = {c["id"] for c in keep}
    keep.sort(key=lambda c: TYPE_ORDER.index(c["type"]))

    # ---- edges: only relationships that stored data backs
    answer_doc = (m.get("answer") or {}).get("document_id")
    doc_to_fact = set()
    for fc in fact_cards:
        if fc["id"] in ids and fc.get("doc_id") and f"doc-{fc['doc_id']}" in ids:
            edges.append({"from": f"doc-{fc['doc_id']}", "to": fc["id"], "kind": "states"})
            doc_to_fact.add(f"doc-{fc['doc_id']}")
        if fc["id"] in ids and fc.get("in_chain"):
            edges.append({"from": fc["id"], "to": "answer", "kind": "chain"})
    for cc in conflict_cards:
        for fid in cc["fact_ids"]:
            if f"fact-{fid}" in ids and cc["id"] in ids:
                edges.append({"from": f"fact-{fid}", "to": cc["id"], "kind": "conflicts"})
    for c in cards:
        if c["id"] in ids and (c["link"]["id"] == answer_doc or c["id"] not in doc_to_fact):
            edges.append({"from": c["id"], "to": "answer", "kind": "cites"})
    for c in keep:
        if c["type"] in ("verified", "review", "used"):
            edges.append({"from": c["id"], "to": "answer", "kind": {"verified": "reviewed", "review": "reviewed", "used": "used"}[c["type"]]})
    edges = [e for e in edges if e["from"] in ids and (e["to"] == "answer" or e["to"] in ids)]
    uniq, seen_e = [], set()
    for e in edges:
        k = (e["from"], e["to"])
        if k not in seen_e:
            seen_e.add(k); uniq.append(e)

    ver_ans = (m.get("verified") or {}).get("answer")
    text = m.get("corrected_answer") or ver_ans or (m.get("answer") or {}).get("text") or m.get("answer_plain") or ""
    for c in keep:
        for k in ("fact_id", "doc_id", "in_chain", "fact_ids", "conflict_id", "is_answer_doc"):
            c.pop(k, None)
    return {"qa_id": m.get("qa_id"), "question": m.get("question"), "answer": text, "state": m.get("state"),
            "cards": keep, "edges": uniq, "columns": 3, "limit": MAX_CARDS}


@router.get("/ask/{qa_id}/evidence")
def answer_evidence(qa_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    from . import main as _main                                  # lazy: main imports this module
    qa = db.get(models.QARecord, qa_id)
    if not qa or qa.org_id != user.org_id or qa.user_id != user.id:
        raise HTTPException(404, "Answer not found")
    m = _main._message_out(db, user, qa, {})                     # re-checks document permissions right now
    return build_evidence(db, user, m, qa)


# ------------------------------------------------------- search vs Anamnesis ---

_EXTRA_STOP = {"how", "does", "what", "when", "where", "which", "who", "why", "often", "much", "many", "can", "the", "our"}


def _tokens(q: str):
    out = []
    for w in re.findall(r"[a-z0-9]+", (q or "").lower()):
        if len(w) >= 3 and w not in nlp.STOP and w not in _EXTRA_STOP and w not in out:
            out.append(w)
    return out


@router.get("/evidence/compare")
def compare(q: str = Query(..., min_length=2, max_length=300), user: models.User = Depends(auth.get_current_user),
            db: Session = Depends(get_db)):
    from . import main as _main
    q = q.strip()
    toks = _tokens(q)
    chunks, visible = retrieval.authorized_chunks(db, user)       # permission gate BEFORE anything is counted
    hit = lambda text: any(t in (text or "").lower() for t in toks)  # noqa: E731
    n_docs = sum(1 for d in visible.values() if hit(d.title) or hit(d.content))
    n_pass = sum(1 for c in chunks if hit(c.text))
    n_facts = sum(1 for f in graph.visible_facts(db, user) if hit(f"{f.subject} {f.relation} {f.object}"))
    plain = {"total": n_docs + n_pass + n_facts,
             "rows": [{"label": "Documents", "count": n_docs}, {"label": "Passages", "count": n_pass},
                      {"label": "Facts", "count": n_facts}],
             "missing": ["No context", "No relationships", "No proof"],
             "note": "Keyword matches in everything you may open."}

    res = retrieval.search_grouped(db, user, q)
    hop = graph.multi_hop_answer(db, user, q)
    found = retrieval.find_verified(db, user, q)
    verified = _main._verified_block(db, found[0], found[1]) if found else None
    answer = res["answer"]
    text = verified["answer"] if verified else (answer["text"] if answer else (("Chain: " + hop["path"]) if hop else None))
    conf = features.compute_confidence(db, answer, res["documents"], verified)
    state = impact.answer_state(db, bool(text), verified, None, res["documents"], hop, conf)
    m = {"qa_id": None, "question": q, "answer": answer, "answer_plain": text, "verified": verified,
         "documents": res["documents"], "multi_hop": hop, "state": state}
    return {"query": q, "plain": plain, "anamnesis": build_evidence(db, user, m, None)}


# ---------------------------------------------------------------- graph data ---

@router.get("/evidence/graph")
def evidence_graph(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    docs, names = _Docs(db, user), {}
    nodes, edges, depts = {}, [], set()
    for f in graph.visible_facts(db, user):
        d = _fact_doc(db, docs, f)
        dept = d.department if d is not None else None
        if dept:
            depts.add(dept)
        for label in (f.subject, f.object):
            nodes.setdefault(label, {"id": label, "label": label, "conflict": False, "degree": 0})
            nodes[label]["degree"] += 1
            if f.status == "conflicting":
                nodes[label]["conflict"] = True
        edges.append({
            "id": f.id, "from": f.subject, "to": f.object, "label": f.relation, "status": f.status,
            "department": dept, "added_by": _user_name(db, f.created_by, names), "created_at": _iso(f.created_at),
            "doc": ({"id": d.id, "title": d.title, "department": d.department, "visibility": d.visibility}
                    if d is not None else None),
        })
    return {"nodes": list(nodes.values()), "edges": edges, "departments": sorted(depts),
            "statuses": sorted({e["status"] for e in edges})}
