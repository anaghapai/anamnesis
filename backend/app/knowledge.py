"""Ask the Organization  +  related questions  +  Starting Point.

When nothing written down can answer a question, Anamnesis can route it to a PERSON - but it never
guesses access. A suggestion must be both
   relevant   (a graph fact names them, they own a related document, they verified a similar answer) and
   authorized (they may open the knowledge area the question belongs to - re-checked on every step).
Only the question text travels; no documents are attached. The answer is verified by a manager and only
then becomes organizational memory (a verified QARecord that is served first next time, within the
department it was shared with). Nothing here uses a generative model.
"""
import re
import json
import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import models, auth, retrieval, graph, nlp
from .db import get_db

router = APIRouter()
MANAGER = auth.ROLE_RANK["manager"]
ADMIN = auth.ROLE_RANK["admin"]
OPEN = ("assigned", "answered", "verification_pending")


# ----------------------------------------------------------------- helpers ---

def _now():
    return datetime.datetime.utcnow()


def _iso(dt):
    return dt.isoformat() if dt else None


def _norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _log(db: Session, org_id, user_id, action, detail=""):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action, detail=detail))


def _notify(db: Session, org_id, from_id, to_id, kind, text, qa_id=None):
    if to_id and to_id != from_id:
        db.add(models.WorkUpdate(org_id=org_id, from_user_id=from_id, to_user_id=to_id, kind=kind, text=text,
                                 qa_id=qa_id))


def _names(db: Session, org_id: int) -> dict:
    return {u.id: u.name for u in db.query(models.User).filter_by(org_id=org_id)}


def _supervisor(db: Session, user: models.User) -> Optional[models.User]:
    if user.supervisor_id:
        s = db.get(models.User, user.supervisor_id)
        if s and s.active:
            return s
    mgr = db.query(models.User).filter(
        models.User.org_id == user.org_id, models.User.department == user.department,
        models.User.role == "manager", models.User.active == True, models.User.id != user.id).first()  # noqa: E712
    if mgr:
        return mgr
    return db.query(models.User).filter(models.User.org_id == user.org_id,
                                        models.User.role.in_(["admin", "owner"]), models.User.id != user.id).first()


def _authorized(db: Session, u: models.User, scope: List[str]) -> bool:
    """Re-checked every time: may this person open ALL the departments of the knowledge area?"""
    return all(retrieval.dept_ok(u, d, db) for d in scope)


def _topic(question: str) -> List[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in nlp.STOP and len(w) > 2]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:3]


def _visible_facts(db: Session, user: models.User) -> List[models.Fact]:
    """Facts whose source document (if any) this person may open."""
    cache, out = {}, []
    for f in graph.visible_facts(db, user):
        if f.document_id is not None:
            if f.document_id not in cache:
                d = db.get(models.Document, f.document_id)
                cache[f.document_id] = bool(d is not None and d.deleted_at is None and (d.workspace or "company") == "company"
                                            and retrieval.user_can_see_document(user, d, db))
            if not cache[f.document_id]:
                continue
        out.append(f)
    return out


def _area(db: Session, asker: models.User, question: str):
    """The knowledge area of a question, seen ONLY through what the asker may open:
    departments of the matching documents / facts. Nothing hidden is ever consulted."""
    res = retrieval.search_grouped(db, asker, question)
    docs = {g["document_id"]: g for g in res["documents"]}
    scope = {g["department"] or "All" for g in res["documents"]}
    kws = [w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in nlp.STOP and len(w) > 3]
    kws = list(set(kws) | {w for w in nlp.expand_words(question) if len(w) >= 4 and w not in nlp.STOP})
    facts = []
    for f in _visible_facts(db, asker):
        lab = f"{f.subject} {f.object}".lower()
        if kws and any(k in lab for k in kws):
            facts.append(f)
            if f.document_id is None:
                scope.add("All")
            else:
                d = db.get(models.Document, f.document_id)
                scope.add(d.department or "All")
    return sorted(scope), docs, facts


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def _candidates(db: Session, asker: models.User, question: str):
    """Everybody authorized for the knowledge area, each with a relevance score and the reasons for it."""
    scope, docs, facts = _area(db, asker, question)
    topic = _topic(question)
    if not scope:
        return scope, topic, []
    qu = nlp.analyze(question)[0]
    past = [r for r in db.query(models.QARecord).filter(
        models.QARecord.org_id == asker.org_id, models.QARecord.status.in_(["verified", "corrected"]),
        models.QARecord.reviewer_id.isnot(None)) if _dice(qu, nlp.analyze(r.question)[0]) >= 0.4
        and retrieval.dept_ok(asker, r.department)]
    doc_objs = {i: db.get(models.Document, i) for i in docs}
    out = []
    for u in db.query(models.User).filter(models.User.org_id == asker.org_id, models.User.active == True,  # noqa: E712
                                          models.User.id != asker.id):
        if u.role == "guest" or u.pending_approval or not _authorized(db, u, scope):
            continue
        score, reasons = 0.0, []
        for f in facts:
            if u.name.strip().lower() in (f.subject.strip().lower(), f.object.strip().lower()):
                score += 3.0
                reasons.append(f"graph fact: {f.subject} {f.relation} {f.object}")
                break
        for i, d in doc_objs.items():
            if d is None or not retrieval.user_can_see_document(u, d, db):
                continue
            if d.owner_id == u.id:
                score += 2.5
                reasons.append(f"owns related document '{d.title}'")
            elif d.uploaded_by == u.id:
                score += 1.5
                reasons.append(f"uploaded related document '{d.title}'")
        if any(r.reviewer_id == u.id for r in past):
            score += 2.0
            reasons.append("verified a similar answer before")
        out.append({"id": u.id, "name": u.name, "role": u.role, "department": u.department,
                    "score": round(score, 1), "reasons": reasons or ["authorized for this knowledge area"]})
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return scope, topic, out


def _experts(cands, scope):
    area = ", ".join(scope)
    top = [c for c in cands if c["score"] > 0][:3]
    for c in top:
        c["why"] = f"Suggested because {'; '.join(c['reasons'])}, and is authorized for this knowledge area ({area})."
    return top


# ------------------------------------------------------------------ schemas ---

class ExpertsIn(BaseModel):
    question: str
    qa_id: Optional[int] = None


class RequestIn(BaseModel):
    question: str
    qa_id: Optional[int] = None
    assignee_id: Optional[int] = None


class AnswerIn(BaseModel):
    answer: str
    verify: bool = True
    share_with: str = "department"


class DeclineIn(BaseModel):
    note: Optional[str] = ""


class ForwardIn(BaseModel):
    to_user_id: int
    note: Optional[str] = ""


class FactIn(BaseModel):
    subject: str
    relation: str
    object: str


class VerifyIn(BaseModel):
    approve: bool
    note: Optional[str] = ""
    share_with: str = "department"
    fact: Optional[FactIn] = None


def _own_qa(db: Session, user, qa_id):
    if qa_id is None:
        return None
    qa = db.get(models.QARecord, qa_id)
    if not qa or qa.org_id != user.org_id or qa.user_id != user.id:
        raise HTTPException(404, "Question not found")
    return qa


# ------------------------------------------------------------------ experts ---

@router.post("/knowledge/experts")
def find_experts(req: ExpertsIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    q = req.question.strip()
    if not q:
        raise HTTPException(400, "Type a question first")
    _own_qa(db, user, req.qa_id)
    scope, topic, cands = _candidates(db, user, q)
    experts = _experts(cands, scope)
    sup = _supervisor(db, user)
    return {"topic": ", ".join(topic) or "your question", "scope": scope, "experts": experts,
            "fallback": {"id": sup.id, "name": sup.name, "role": sup.role} if sup else None,
            "message": None if experts else "No expert could be suggested for this. You can send the question to your supervisor."}


# ----------------------------------------------------------------- requests ---

def _forwards(r) -> list:
    try:
        return json.loads(r.forwards or "[]")
    except Exception:
        return []


def _scope_of(r) -> List[str]:
    return [s for s in (r.scope or "").split(",") if s]


def _can_self_verify(u: models.User, r: models.KnowledgeRequest, db: Session) -> bool:
    return auth.rank(u) >= MANAGER and u.id != r.requester_id and _authorized(db, u, _scope_of(r))


def _can_verify(u: models.User, r: models.KnowledgeRequest, db: Session) -> bool:
    return (r.status == "verification_pending" and u.org_id == r.org_id and auth.rank(u) >= MANAGER
            and u.id not in (r.requester_id, r.answered_by) and _authorized(db, u, _scope_of(r)))


def _out(db: Session, r: models.KnowledgeRequest, me: models.User, names: dict) -> dict:
    return {"id": r.id, "question": r.question, "status": r.status, "requester": names.get(r.requester_id, "—"),
            "requester_id": r.requester_id, "created_at": _iso(r.created_at), "reason": r.reason,
            "assigned_to": names.get(r.assignee_id), "route": r.route, "answer": r.answer,
            "answered_by": names.get(r.answered_by), "verified_by": names.get(r.verified_by),
            "share_with": r.share_with,
            "forwards": [{"from": names.get(f.get("from_id"), "—"), "to": names.get(f.get("to_id"), "—"),
                          "note": f.get("note")} for f in _forwards(r)],
            "can_answer": r.status == "assigned" and r.assignee_id == me.id,
            "can_self_verify": _can_self_verify(me, r, db)}


@router.post("/knowledge/requests")
def create_request(req: RequestIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    q = req.question.strip()
    if not q:
        raise HTTPException(400, "Type a question first")
    _own_qa(db, user, req.qa_id)
    names = _names(db, user.org_id)
    for r in db.query(models.KnowledgeRequest).filter(models.KnowledgeRequest.requester_id == user.id,
                                                      models.KnowledgeRequest.status.in_(OPEN)):
        if _norm(r.question) == _norm(q):
            return {"id": r.id, "status": r.status, "assigned_to": names.get(r.assignee_id), "route": r.route,
                    "duplicate": True}
    scope, topic, cands = _candidates(db, user, q)
    experts = _experts(cands, scope)
    if req.assignee_id is None:
        target = _supervisor(db, user)
        if not target:
            raise HTTPException(400, "You have no supervisor to send this to")
        route, reason = "supervisor", "your supervisor"
    else:
        pick = next((e for e in experts if e["id"] == req.assignee_id), None)
        if pick is None:
            raise HTTPException(403, "That person is not an eligible expert for this question (either not relevant, "
                                     "or not authorized for this knowledge area).")
        target = db.get(models.User, pick["id"])
        route, reason = "expert", "; ".join(pick["reasons"])
    r = models.KnowledgeRequest(org_id=user.org_id, requester_id=user.id, qa_id=req.qa_id, question=q,
                                department=user.department, scope=",".join(scope), assignee_id=target.id,
                                route=route, reason=reason)
    db.add(r)
    db.flush()
    _notify(db, user.org_id, user.id, target.id, "review_request",
            f"{user.name} needs your knowledge: “{q}”. No documents are attached — answer from what you know.",
            qa_id=req.qa_id)
    _log(db, user.org_id, user.id, "knowledge_request", f"#{r.id} to {target.name} ({route}): {q}")
    db.commit()
    return {"id": r.id, "status": r.status, "assigned_to": target.name, "route": route}


@router.get("/knowledge/requests")
def list_requests(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    rows = db.query(models.KnowledgeRequest).filter_by(org_id=user.org_id).order_by(
        models.KnowledgeRequest.id.desc()).all()
    assigned, verify, mine, handled = [], [], [], []
    for r in rows:
        if r.requester_id == user.id:
            mine.append(_out(db, r, user, names))
            continue
        if r.assignee_id == user.id and r.status == "assigned":
            assigned.append(_out(db, r, user, names))
        elif _can_verify(user, r, db):
            verify.append(_out(db, r, user, names))
        elif user.id in (r.answered_by, r.verified_by) or r.assignee_id == user.id \
                or any(f.get("from_id") == user.id for f in _forwards(r)):
            handled.append(_out(db, r, user, names))
    return {"assigned": assigned, "verify": verify, "mine": mine[:30], "handled": handled[:30]}


def _mine_to_answer(db: Session, user, rid: int) -> models.KnowledgeRequest:
    r = db.get(models.KnowledgeRequest, rid)
    if not r or r.org_id != user.org_id:
        raise HTTPException(404, "Request not found")
    if r.status != "assigned" or r.assignee_id != user.id:
        raise HTTPException(403, "This request isn't waiting on you")
    return r


def _shared_dept(user: models.User, r: models.KnowledgeRequest, share_with: str) -> str:
    """Company-wide only when the verifier may speak for the whole knowledge area."""
    if share_with == "company":
        scope = _scope_of(r)
        if auth.rank(user) >= ADMIN or (auth.rank(user) >= MANAGER and (not scope or scope == ["All"])):
            return "All"
    return r.department


def _finalize(db: Session, r: models.KnowledgeRequest, verifier: models.User, note: str, share_with: str,
              fact: Optional[FactIn]):
    names = _names(db, r.org_id)
    dept = _shared_dept(verifier, r, share_with)
    qa = db.get(models.QARecord, r.qa_id) if r.qa_id else None
    if qa and qa.org_id == r.org_id and qa.user_id == r.requester_id and not qa.answer_text:
        pass                                               # fill the unanswered chat message in place
    else:
        qa = models.QARecord(org_id=r.org_id, user_id=r.requester_id, question=r.question, sources="[]")
        db.add(qa)
    qa.department = dept
    qa.answer_text = qa.corrected_answer = r.answer
    qa.status, qa.reviewer_id, qa.reviewed_at = "corrected", verifier.id, _now()
    qa.review_note = (note or "").strip() or f"Answered by {names.get(r.answered_by)}, verified by {verifier.name}"
    db.flush()
    r.status, r.verified_by, r.verified_at, r.verify_note = "verified", verifier.id, _now(), note
    r.result_qa_id = qa.id
    r.share_with = "company" if dept == "All" else "department"
    text = (f"{verifier.name} verified the answer to “{r.question}”: {r.answer} "
            f"It is now organizational knowledge — ask the same question again and it comes first.")
    _notify(db, r.org_id, verifier.id, r.requester_id, "review_result", text, qa_id=qa.id)
    if r.answered_by and r.answered_by != verifier.id:
        _notify(db, r.org_id, verifier.id, r.answered_by, "review_result",
                f"{verifier.name} verified your answer to “{r.question}”.", qa_id=qa.id)
    _log(db, r.org_id, verifier.id, "knowledge_verified", f"#{r.id} -> answer #{qa.id} ({r.share_with}): {r.question}")
    if fact and fact.subject.strip() and fact.relation.strip() and fact.object.strip():
        graph.add_fact(db, verifier, fact.subject.strip(), fact.relation.strip(), fact.object.strip())
    db.commit()


@router.post("/knowledge/requests/{rid}/answer")
def answer_request(rid: int, req: AnswerIn, user: models.User = Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    r = _mine_to_answer(db, user, rid)
    ans = req.answer.strip()
    if len(ans) < 3:
        raise HTTPException(400, "Write an answer first")
    if req.share_with not in ("department", "company"):
        raise HTTPException(400, "share_with must be department or company")
    r.answer, r.answered_by, r.answered_at = ans, user.id, _now()
    r.share_with = req.share_with
    if not req.verify:
        r.status = "answered"
        _notify(db, r.org_id, user.id, r.requester_id, "review_result",
                f"{user.name} answered “{r.question}”: {ans} (not verified — not stored as organizational knowledge).",
                qa_id=r.qa_id)
        _log(db, r.org_id, user.id, "knowledge_answered", f"#{r.id}")
        db.commit()
        return {"status": r.status}
    if _can_self_verify(user, r, db):
        _finalize(db, r, user, "", req.share_with, None)
        return {"status": r.status}
    r.status = "verification_pending"
    verifiers = [u for u in db.query(models.User).filter(models.User.org_id == r.org_id,
                                                          models.User.active == True)  # noqa: E712
                 if _can_verify(u, r, db)]
    sup = _supervisor(db, user)
    pick = next((u for u in verifiers if sup and u.id == sup.id), verifiers[0] if verifiers else None)
    if pick:
        _notify(db, r.org_id, user.id, pick.id, "review_request",
                f"{user.name} answered “{r.question}” — please verify it.", qa_id=r.qa_id)
    _log(db, r.org_id, user.id, "knowledge_answered", f"#{r.id} (awaiting verification)")
    db.commit()
    return {"status": r.status}


@router.post("/knowledge/requests/{rid}/decline")
def decline_request(rid: int, req: DeclineIn, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    r = _mine_to_answer(db, user, rid)
    r.status, r.verify_note = "declined", req.note
    _notify(db, r.org_id, user.id, r.requester_id, "review_result",
            f"{user.name} couldn't answer “{r.question}”." + (f" Note: {req.note}" if req.note else "")
            + " You can ask someone else.", qa_id=r.qa_id)
    _log(db, r.org_id, user.id, "knowledge_declined", f"#{r.id}")
    db.commit()
    return {"status": r.status}


def _forward_candidates(db: Session, me: models.User, r: models.KnowledgeRequest):
    requester = db.get(models.User, r.requester_id)
    scope, _, cands = _candidates(db, requester, r.question)
    return [c for c in cands if c["id"] not in (me.id, r.requester_id)]


@router.get("/knowledge/requests/{rid}/forward-options")
def forward_options(rid: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    r = _mine_to_answer(db, user, rid)
    return {"options": _forward_candidates(db, user, r)}


@router.post("/knowledge/requests/{rid}/forward")
def forward_request(rid: int, req: ForwardIn, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    r = _mine_to_answer(db, user, rid)
    if req.to_user_id in (r.requester_id, user.id):
        raise HTTPException(400, "Choose someone else")
    pick = next((c for c in _forward_candidates(db, user, r) if c["id"] == req.to_user_id), None)
    if pick is None:
        raise HTTPException(403, "That person is not authorized for this knowledge area")
    fw = _forwards(r)
    fw.append({"from_id": user.id, "to_id": pick["id"], "note": req.note or "", "at": _iso(_now())})
    r.forwards, r.assignee_id = json.dumps(fw), pick["id"]
    r.reason = f"forwarded by {user.name}" + (f" — {req.note}" if req.note else "")
    _notify(db, r.org_id, user.id, pick["id"], "review_request",
            f"{user.name} forwarded a question to you: “{r.question}”. No documents are attached.", qa_id=r.qa_id)
    _log(db, r.org_id, user.id, "knowledge_forwarded", f"#{r.id} -> {pick['name']}")
    db.commit()
    return {"status": r.status, "assigned_to": pick["name"]}


@router.post("/knowledge/requests/{rid}/verify")
def verify_request(rid: int, req: VerifyIn, user: models.User = Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    r = db.get(models.KnowledgeRequest, rid)
    if not r or r.org_id != user.org_id or not _can_verify(user, r, db):
        raise HTTPException(404, "Nothing to verify here (or it isn't yours to verify)")
    if req.share_with not in ("department", "company"):
        raise HTTPException(400, "share_with must be department or company")
    if not req.approve:
        r.status, r.verified_by, r.verified_at, r.verify_note = "rejected", user.id, _now(), req.note
        for uid in {r.requester_id, r.answered_by}:
            _notify(db, r.org_id, user.id, uid, "review_result",
                    f"{user.name} did not verify the answer to “{r.question}”." + (f" Note: {req.note}" if req.note else ""),
                    qa_id=r.qa_id)
        _log(db, r.org_id, user.id, "knowledge_rejected", f"#{r.id}")
        db.commit()
        return {"status": r.status}
    _finalize(db, r, user, req.note or "", req.share_with, req.fact)
    return {"status": r.status}


# ------------------------------------------------- related / Starting Point ---

def _question_for(f: models.Fact, entity: str) -> Optional[str]:
    rel = f.relation.strip().lower()
    if entity == f.subject:
        return {"part_of": f"What is {entity} part of?",
                "deadline": f"What is the deadline for {entity}?",
                "sponsored_by": f"Who sponsors {entity}?",
                "owns": f"What does {entity} own?",
                "raised_concern_about": f"What did {entity} raise concerns about?"}.get(
            rel, f"What is {entity}'s {rel.replace('_', ' ')}?")
    return {"owns": f"Who owns {entity}?",
            "raised_concern_about": f"What concerns were raised about {entity}?",
            "part_of": f"What is part of {entity}?",
            "sponsored_by": f"What does {entity} sponsor?"}.get(rel)


def _questions_from(facts: List[models.Fact], entities: List[str], focus: set, skip_units=None, limit=4) -> List[str]:
    ents = sorted(dict.fromkeys(entities), key=lambda e: 0 if focus & set(_norm(e).split()) else 1)
    out, seen = [], set()
    for e in ents:
        for f in facts:
            if e not in (f.subject, f.object):
                continue
            q = _question_for(f, e)
            if not q or q in seen:
                continue
            if skip_units and _dice(skip_units, nlp.analyze(q)[0]) >= 0.85:      # don't repeat the question just asked
                continue
            seen.add(q)
            out.append(q)
            if len(out) >= limit:
                return out
    return out


@router.get("/knowledge/related/{qa_id}")
def related(qa_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    qa = db.get(models.QARecord, qa_id)
    if not qa or qa.org_id != user.org_id or qa.user_id != user.id:
        raise HTTPException(404, "Question not found")
    facts = _visible_facts(db, user)
    by_id = {f.id: f for f in facts}
    try:
        srcs = json.loads(qa.sources or "[]")
    except Exception:
        srcs = []
    entities, cited = [], {s.get("document_id") for s in srcs if s.get("document_id") is not None}
    for s in srcs:
        for h in s.get("hops", []) if s.get("kind") == "hop" else []:
            f = by_id.get(h.get("fact_id"))
            if f:
                entities += [f.subject, f.object]
    for f in facts:
        if f.document_id in cited:
            entities += [f.subject, f.object]
    focus = {w for w in _norm(qa.question).split() if len(w) > 3}
    qs = _questions_from(facts, entities, focus, nlp.analyze(qa.question)[0])
    return {"questions": qs}


def _onboarding(db: Session, target: models.User) -> dict:
    names = _names(db, target.org_id)
    docs = [d for d in db.query(models.Document).filter_by(org_id=target.org_id).order_by(models.Document.id.desc())
            if d.deleted_at is None and (d.workspace or "company") == "company"
            and retrieval.user_can_see_document(target, d, db)]

    def rank_doc(d):
        return 0 if d.department == target.department else 1 if d.department in ("All", None) else 2
    docs.sort(key=rank_doc)
    why = {0: "Your department's knowledge", 1: "Company-wide", 2: "Shared with you"}
    shown = docs[:8]
    topics = []
    for d in shown:
        for t in (d.tags or "").split(","):
            t = t.strip()
            if t and t not in topics:
                topics.append(t)
    sup = db.get(models.User, target.supervisor_id) if target.supervisor_id else _supervisor(db, target)
    people, seen = [], {target.id}

    def add_person(uid, why_text):
        if uid and uid not in seen:
            u = db.get(models.User, uid)
            if u and u.active:
                seen.add(uid)
                people.append({"id": u.id, "name": u.name, "role": u.role, "department": u.department,
                               "why": why_text})
    if sup:
        add_person(sup.id, "Your supervisor")
    facts = _visible_facts(db, target)
    by_name = {u.name.strip().lower(): u.id for u in db.query(models.User).filter_by(org_id=target.org_id)}
    for f in facts:
        uid = by_name.get(f.subject.strip().lower())
        if uid and len(people) < 5:
            add_person(uid, f"{f.relation.replace('_', ' ').capitalize()} {f.object} (knowledge graph)")
    for d in shown[:5]:
        if len(people) < 5 and d.owner_id:
            add_person(d.owner_id, f"Owns '{d.title}'")
    entities = [e for f in facts for e in (f.subject, f.object)]
    qs = _questions_from(facts, entities, set(), None, limit=5)
    qs += [f"What are the key points of '{d.title}'?" for d in shown[:3]]
    return {"name": target.name, "role": target.role, "department": target.department,
            "supervisor": names.get(target.supervisor_id),
            "topics": topics[:8],
            "documents": [{"id": d.id, "title": d.title, "department": d.department, "why": why[rank_doc(d)]}
                          for d in shown],
            "experts": people, "questions": qs[:8]}


@router.get("/onboarding")
def my_start(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return _onboarding(db, user)


@router.get("/onboarding/{user_id}")
def their_start(user_id: int, user: models.User = Depends(auth.require_role("manager")),
                db: Session = Depends(get_db)):
    t = db.get(models.User, user_id)
    if not t or t.org_id != user.org_id or not t.active:
        raise HTTPException(404, "Person not found")
    return _onboarding(db, t)
