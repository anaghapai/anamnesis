"""Insights pack: criteria-based "needs review", reporting-line routing, corrections that feed the graph,
and the "I used this" trace.

  * Needs-review LOGIC (replaces "every document with no verified_until is flagged")
  * Ask-for-review -> owner -> assign a reviewer -> escalate up the reporting line if nobody acts
  * Last Known Truth, knowledge-gap learning, recall notices, verified-answer invalidation
  * Dependency impact explorer, blast-radius queue, what-if policy preview
  * Expertise decay + knowledge bus factor, organizational memory diff
  * Conflict arbitration hint, "request access instead of a dead end"

Nothing here calls a generative model - it is all rules and arithmetic, and every rule is explained
to the user. Permission checks always go through retrieval.user_can_see_document.
"""
import os
import json
import datetime
from types import SimpleNamespace
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from . import models, auth, retrieval, nlp, features
from .db import get_db, SessionLocal

router = APIRouter(prefix="/insights", tags=["insights"])

ESC_HOURS = float(os.environ.get("ANAMNESIS_ESCALATE_HOURS", "24"))   # time before a flag/request moves up a level
REVIEW_DAYS = 90        # a document nobody has reviewed for this long "needs review"
GAP_MIN = 2             # a question asked this many times without a verified answer is a knowledge gap
GAP_AUTO = 3            # ...and is sent to the department manager automatically at this many
HALF_LIFE = 90.0        # expertise loses half its weight every 90 days
EXPERT_MIN = 1.0        # score needed to count as a *current* expert
MANAGER = auth.ROLE_RANK["manager"]
ADMIN = auth.ROLE_RANK["admin"]
SEV = {"high": 3, "medium": 2, "low": 1}

# ------------------------------------------------------------------ schema ---

_NEW_COLUMNS = {
    "facts": [("superseded_at", "DATETIME")],
    "conflicts": [("resolved_at", "DATETIME")],
    "documents": [("content_changed_at", "DATETIME")],
    "qa_records": [("flagged_at", "DATETIME"), ("esc_level", "INTEGER DEFAULT 0"), ("esc_due", "DATETIME"),
                   ("needs_rereview", "BOOLEAN DEFAULT 0"), ("rereview_reason", "TEXT")],
}


def ensure_schema(engine):
    """Adds the new columns to an existing anamnesis.db (no data loss) and back-fills the timestamps."""
    with engine.begin() as con:
        for table, cols in _NEW_COLUMNS.items():
            existing = {r[1] for r in con.execute(sql_text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue
            for name, coltype in cols:
                if name not in existing:
                    con.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))
        con.execute(sql_text(
            "UPDATE facts SET superseded_at = COALESCE((SELECT MAX(c.created_at) FROM conflicts c "
            "WHERE c.old_fact_id = facts.id OR c.new_fact_id = facts.id), created_at) "
            "WHERE status = 'superseded' AND superseded_at IS NULL"))
        con.execute(sql_text("UPDATE qa_records SET esc_level = 0 WHERE esc_level IS NULL"))
        con.execute(sql_text("UPDATE qa_records SET needs_rereview = 0 WHERE needs_rereview IS NULL"))


# ----------------------------------------------------------------- helpers ---

def _now():
    return datetime.datetime.utcnow()


def _iso(x):
    return x.isoformat() if x else None


def _users(db, org_id):
    return {u.id: u for u in db.query(models.User).filter_by(org_id=org_id)}


def _audit(db, org_id, user_id, action, detail=""):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action, detail=detail))


def _notify(db, org_id, from_id, to_id, kind, text, document_id=None, qa_id=None):
    if to_id and from_id and to_id != from_id:
        db.add(models.WorkUpdate(org_id=org_id, from_user_id=from_id, to_user_id=to_id, kind=kind,
                                 text=text, document_id=document_id, qa_id=qa_id))


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def _tok(q):
    return nlp.analyze(q)[0]


def _sources(src_json):
    """-> (document ids, fact ids) an answer was built from."""
    docs, facts = set(), set()
    try:
        for s in json.loads(src_json or "[]"):
            if s.get("kind") == "hop":
                facts |= {h.get("fact_id") for h in s.get("hops", []) if h.get("fact_id")}
            elif s.get("document_id") is not None:
                docs.add(s["document_id"])
    except Exception:
        pass
    return docs, facts


def _company_docs(db, org_id):
    return [d for d in db.query(models.Document).filter(models.Document.org_id == org_id,
                                                        models.Document.deleted_at.is_(None)).all()
            if (d.workspace or "company") == "company"]


def _visible_doc(db, user, doc_id):
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id or d.deleted_at is not None or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    return d


def _build_chain(db, org_id, first_ids, dept, exclude):
    """Reporting-line chain: first person -> department manager(s) -> admin/owner."""
    users = _users(db, org_id)
    chain = []

    def add(uid):
        u = users.get(uid)
        if u and u.active and uid not in exclude and uid not in chain:
            chain.append(uid)
    for uid in first_ids:
        add(uid)
    for u in features._approvers(db, org_id, dept):
        add(u.id)
    for u in sorted(users.values(), key=lambda x: -auth.rank(x)):
        if u.role in ("admin", "owner"):
            add(u.id)
    return chain


def _supervisor_id(db, user):
    if user.supervisor_id:
        s = db.query(models.User).get(user.supervisor_id)
        if s and s.active:
            return s.id
    return None


# ------------------------------------------------- Phase 0: fact <-> docs ---

_LINK_SIG = {}


def _link_sig(db, org_id):
    f = db.query(models.Fact).filter_by(org_id=org_id)
    d = db.query(models.Document).filter_by(org_id=org_id)
    return (f.count(), max([x.id for x in f] or [0]), d.count(),
            sum(len(x.content or "") for x in d), sum(1 for x in d if x.deleted_at is not None))


def ensure_links(db, org_id, force=False):
    """(Re)build the document-to-fact link table when facts or documents changed."""
    sig = _link_sig(db, org_id)
    if not force and _LINK_SIG.get(org_id) == sig:
        return
    db.query(models.FactDocLink).filter_by(org_id=org_id).delete()
    docs = _company_docs(db, org_id)
    lows = {d.id: (d.content or "").lower() for d in docs}
    for f in db.query(models.Fact).filter_by(org_id=org_id):
        labels = [x.strip().lower() for x in (f.subject, f.object) if len(x.strip()) >= 4]
        for d in docs:
            if f.document_id == d.id:
                db.add(models.FactDocLink(org_id=org_id, fact_id=f.id, document_id=d.id, kind="source"))
            elif any(l in lows[d.id] for l in labels):
                db.add(models.FactDocLink(org_id=org_id, fact_id=f.id, document_id=d.id, kind="mention"))
    db.commit()
    _LINK_SIG[org_id] = _link_sig(db, org_id)


def invalidate_links(org_id):
    _LINK_SIG.pop(org_id, None)


# ---------------------------------------------- needs-review LOGIC (rules) ---

def review_info(db: Session, org_id: int, docs) -> dict:
    """Decide, per document, whether it really needs review - and say WHY.
    Rules (any one is enough):
      high   : an open conflict involves a fact from this document
      high   : it still states a fact that was replaced after its last review
      high   : a verified answer built on it is waiting for re-review
      high   : someone asked for a review and nobody has finished it
      medium : its verification date has passed
      medium : answers citing it are flagged as wrong/outdated
      medium : its owner is missing or deactivated
      low    : never explicitly verified and not reviewed for 90+ days
    A brand-new upload is NOT flagged - the old rule flagged everything without a verified_until."""
    now = _now()
    users = _users(db, org_id)
    out = {d.id: [] for d in docs}
    lastrev = {d.id: (d.last_reviewed_at or d.created_at or now) for d in docs}

    def add(did, code, text, sev):
        if did in out:
            out[did].append({"code": code, "text": text, "severity": sev})

    facts = {f.id: f for f in db.query(models.Fact).filter_by(org_id=org_id)}
    for c in db.query(models.Conflict).filter_by(org_id=org_id, resolved=False):
        o, n = facts.get(c.old_fact_id), facts.get(c.new_fact_id)
        if not o or not n:
            continue
        for f in (o, n):
            if f.document_id:
                add(f.document_id, "conflict",
                    f"Conflicting sources: '{o.subject} {o.relation}' is '{o.object}' in one place and '{n.object}' in another", "high")
    for f in facts.values():
        if f.status == "superseded" and f.document_id in out and f.superseded_at and f.superseded_at > lastrev[f.document_id]:
            add(f.document_id, "superseded",
                f"Still states '{f.subject} {f.relation} {f.object}', replaced on {f.superseded_at:%d %b %Y}", "high")

    flagged = {}
    for qa in db.query(models.QARecord).filter(models.QARecord.org_id == org_id):
        if not (qa.status == "flagged" or qa.needs_rereview):
            continue
        for did in _sources(qa.sources)[0]:
            if qa.needs_rereview:
                add(did, "rereview", f"A verified answer built on it needs re-review: \"{qa.question[:70]}\"", "high")
            else:
                flagged[did] = flagged.get(did, 0) + 1
    for did, n in flagged.items():
        add(did, "flagged", f"{n} answer{'s' if n > 1 else ''} citing it {'are' if n > 1 else 'is'} flagged as wrong or outdated", "medium")

    for r in db.query(models.DocReviewRequest).filter(models.DocReviewRequest.org_id == org_id,
                                                      models.DocReviewRequest.status.in_(["open", "assigned"])):
        who = users.get(r.requested_by)
        add(r.document_id, "requested", f"{who.name if who else 'Someone'} asked for a review"
            + (f": {r.note.strip()[:80]}" if (r.note or "").strip() else ""), "high")

    for d in docs:
        if d.verified_until and d.verified_until < now:
            add(d.id, "expired", f"Verification expired on {d.verified_until:%d %b %Y}", "medium")
        elif not d.verified_until:
            age = (now - lastrev[d.id]).days
            if age >= REVIEW_DAYS:
                add(d.id, "stale", f"Not reviewed for {age} days", "medium" if age >= 2 * REVIEW_DAYS else "low")
        o = users.get(d.owner_id)
        if not o or not o.active:
            add(d.id, "no_owner", "Its owner is missing or deactivated - assign a new owner", "medium")

    res = {}
    for d in docs:
        seen, reasons = set(), []
        for r in out[d.id]:
            if r["text"] not in seen:
                seen.add(r["text"]); reasons.append(r)
        reasons.sort(key=lambda r: -SEV[r["severity"]])
        state = "needs_review" if reasons else ("verified" if d.verified_until and d.verified_until >= now else "ok")
        res[d.id] = {"needs_review": bool(reasons), "review_state": state, "review_reasons": [r["text"] for r in reasons],
                     "review_severity": reasons[0]["severity"] if reasons else None, "reasons_full": reasons}
    return res


def is_assigned_reviewer(db, user, d) -> bool:
    return db.query(models.DocReviewRequest).filter(
        models.DocReviewRequest.document_id == d.id, models.DocReviewRequest.assignee_id == user.id,
        models.DocReviewRequest.status == "assigned").first() is not None


def _can_assign(user, d) -> bool:
    return features.can_manage(user, d)


def on_document_verified(db, d, user):
    """Close open review requests on this document and tell the people who asked."""
    n = 0
    for r in db.query(models.DocReviewRequest).filter(models.DocReviewRequest.document_id == d.id,
                                                      models.DocReviewRequest.status.in_(["open", "assigned"])):
        r.status, r.completed_by, r.completed_at = "done", user.id, _now()
        _notify(db, d.org_id, user.id, r.requested_by, "review_done", f"'{d.title}' was reviewed by {user.name}.", document_id=d.id)
        n += 1
    return n


class AskReview(BaseModel):
    note: str = ""


class AssignReview(BaseModel):
    assignee_id: int
    note: str = ""


@router.post("/docs/{doc_id}/ask-review")
def ask_review(doc_id: int, req: AskReview, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Any employee who can open a document can say 'this needs a review'. It goes to the document's
    owner; the owner (or a manager) can then assign somebody. If nobody acts it escalates."""
    d = _visible_doc(db, user, doc_id)
    dup = db.query(models.DocReviewRequest).filter(models.DocReviewRequest.document_id == d.id,
                                                   models.DocReviewRequest.status.in_(["open", "assigned"])).first()
    if dup:
        return {"ok": True, "id": dup.id, "routed_to": None, "message": "A review is already open for this document."}
    chain = _build_chain(db, d.org_id, [d.owner_id], d.department, {user.id})
    holder = chain[0] if chain else None
    r = models.DocReviewRequest(org_id=d.org_id, document_id=d.id, requested_by=user.id, note=req.note.strip(),
                                holder_id=holder, esc_due=_now() + datetime.timedelta(hours=ESC_HOURS))
    db.add(r)
    hu = db.query(models.User).get(holder) if holder else None
    if holder:
        _notify(db, d.org_id, user.id, holder, "review_asked",
                f"{user.name} says '{d.title}' needs a review" + (f": {r.note}" if r.note else "") +
                ". Open it in Review Center to do it yourself or assign someone.", document_id=d.id)
    _audit(db, d.org_id, user.id, "review_asked", f"'{d.title}' -> {hu.name if hu else 'nobody'}")
    db.commit()
    return {"ok": True, "id": r.id, "routed_to": hu.name if hu else None}


@router.post("/docs/{doc_id}/assign-review")
def assign_review(doc_id: int, req: AssignReview, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    r = db.query(models.DocReviewRequest).filter(models.DocReviewRequest.document_id == d.id,
                                                 models.DocReviewRequest.status.in_(["open", "assigned"])).first()
    if not (_can_assign(user, d) or (r and r.holder_id == user.id)):
        raise HTTPException(403, "Only the document's owner or a manager can assign a reviewer")
    a = db.query(models.User).get(req.assignee_id)
    if not a or a.org_id != user.org_id or not a.active:
        raise HTTPException(404, "Person not found")
    if not retrieval.user_can_see_document(a, d, db):
        raise HTTPException(400, f"{a.name} can't open this document, so can't review it. Pick someone with access.")
    if not r:
        r = models.DocReviewRequest(org_id=d.org_id, document_id=d.id, requested_by=user.id, note=req.note.strip())
        db.add(r)
    r.status, r.assignee_id, r.assigned_by, r.holder_id = "assigned", a.id, user.id, a.id
    r.esc_due = _now() + datetime.timedelta(hours=ESC_HOURS)
    _notify(db, d.org_id, user.id, a.id, "review_assigned",
            f"{user.name} assigned you to review '{d.title}'" + (f": {req.note.strip()}" if req.note.strip() else "") +
            ". Open the document and press Mark reviewed when done.", document_id=d.id)
    if r.requested_by != user.id:
        _notify(db, d.org_id, user.id, r.requested_by, "review_assigned", f"{a.name} will review '{d.title}'.", document_id=d.id)
    _audit(db, d.org_id, user.id, "review_assigned", f"'{d.title}' -> {a.name}")
    db.commit()
    return {"ok": True, "assignee": a.name}


def suggest_reviewers(db, d, exclude=(), limit=3):
    """Ranked by real activity on this document's topics (uploads, reviews, approvals) with time decay."""
    sc = expertise_scores(db, d.org_id)
    agg, why = {}, {}
    for t in _topics(d):
        for uid, e in sc.get(t, {}).items():
            agg[uid] = agg.get(uid, 0) + e["score"]
            why.setdefault(uid, []).append(t)
    users = _users(db, d.org_id)
    rows = []
    for uid, u in users.items():
        if not u.active or uid in exclude or u.role == "guest":
            continue
        if not retrieval.user_can_see_document(u, d, db):
            continue
        score = agg.get(uid, 0.0) + (0.25 if u.department == d.department and auth.rank(u) >= MANAGER else 0)
        if score == 0 and u.department == d.department:
            score = 0.1                                   # nobody has activity on the topic: fall back to the same team
        if score > 0:
            rows.append({"id": uid, "name": u.name, "role": u.role, "department": u.department, "score": round(score, 2),
                         "why": ("recent work on " + ", ".join(sorted(why[uid]))) if uid in why
                         else ("manager of this department" if auth.rank(u) >= MANAGER else "same department")})
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


@router.get("/docs/{doc_id}/reviewer-suggestions")
def reviewer_suggestions(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    return suggest_reviewers(db, d, exclude={user.id})


@router.get("/review-meta")
def review_meta(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """One call the document list uses: state + reasons + what THIS user may do, per document."""
    run_escalations(db, user.org_id)
    docs = [d for d in _company_docs(db, user.org_id) if retrieval.user_can_see_document(user, d, db)]
    info = review_info(db, user.org_id, docs)
    names = {u.id: u.name for u in _users(db, user.org_id).values()}
    reqs = {}
    for r in db.query(models.DocReviewRequest).filter(models.DocReviewRequest.org_id == user.org_id,
                                                      models.DocReviewRequest.status.in_(["open", "assigned"])):
        reqs[r.document_id] = r
    out = {}
    for d in docs:
        r = reqs.get(d.id)
        out[d.id] = {"state": info[d.id]["review_state"], "severity": info[d.id]["review_severity"],
                     "reasons": info[d.id]["review_reasons"], "can_assign": _can_assign(user, d),
                     "can_review": user.id == d.owner_id or auth.rank(user) >= MANAGER or is_assigned_reviewer(db, user, d),
                     "request": None if not r else {"id": r.id, "status": r.status, "by": names.get(r.requested_by),
                                                     "assignee": names.get(r.assignee_id), "holder": names.get(r.holder_id),
                                                     "due": _iso(r.esc_due), "level": r.esc_level, "note": r.note}}
    return out


@router.get("/review-queue")
def review_queue(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    run_escalations(db, user.org_id)
    docs = [d for d in _company_docs(db, user.org_id) if retrieval.user_can_see_document(user, d, db)]
    info = review_info(db, user.org_id, docs)
    names = {u.id: u.name for u in _users(db, user.org_id).values()}
    mine_assigned = {r.document_id for r in db.query(models.DocReviewRequest).filter_by(
        org_id=user.org_id, assignee_id=user.id, status="assigned")}
    rows = []
    for d in docs:
        i = info[d.id]
        if not i["needs_review"]:
            continue
        scope_ok = (auth.rank(user) >= ADMIN or d.owner_id == user.id or d.id in mine_assigned or
                    (auth.rank(user) >= MANAGER and d.department in ("All", user.department)))
        if not scope_ok:
            continue
        rows.append({"id": d.id, "title": d.title, "department": d.department, "owner": names.get(d.owner_id),
                     "severity": i["review_severity"], "reasons": i["review_reasons"],
                     "assigned_to_me": d.id in mine_assigned, "can_assign": _can_assign(user, d)})
    rows.sort(key=lambda r: (not r["assigned_to_me"], -SEV[r["severity"] or "low"], r["title"]))
    rere = []
    for qa in db.query(models.QARecord).filter_by(org_id=user.org_id, needs_rereview=True):
        if auth.rank(user) >= MANAGER and retrieval.dept_ok(user, qa.department):
            rere.append({"qa_id": qa.id, "question": qa.question, "answer": qa.corrected_answer or qa.answer_text,
                         "reason": qa.rereview_reason, "department": qa.department})
    return {"docs": rows, "rereview": rere}


class Rereview(BaseModel):
    verdict: str                       # keep | correct | retire
    corrected_answer: str = ""
    note: str = ""


@router.post("/rereview/{qa_id}")
def rereview(qa_id: int, req: Rereview, user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    qa = db.query(models.QARecord).get(qa_id)
    if not qa or qa.org_id != user.org_id or not qa.needs_rereview or not retrieval.dept_ok(user, qa.department):
        raise HTTPException(404, "Nothing to re-review here")
    old = qa.corrected_answer or qa.answer_text
    if req.verdict == "keep":
        qa.needs_rereview, qa.rereview_reason = False, None
    elif req.verdict == "correct":
        if not req.corrected_answer.strip():
            raise HTTPException(400, "Write the corrected answer")
        qa.corrected_answer, qa.status = req.corrected_answer.strip(), "corrected"
        qa.needs_rereview, qa.rereview_reason = False, None
        qa.reviewer_id, qa.reviewed_at, qa.review_note = user.id, _now(), req.note or "Re-reviewed after the source changed"
        recall(db, user, qa_ids=_served_chain(db, qa), text=f"the verified answer to \"{qa.question}\" was corrected.",
               old=old, new=qa.corrected_answer)
    elif req.verdict == "retire":
        qa.status, qa.needs_rereview = "rejected", False
        recall(db, user, qa_ids=_served_chain(db, qa), text=f"the verified answer to \"{qa.question}\" was retired (no longer valid).", old=old)
    else:
        raise HTTPException(400, "verdict must be keep, correct or retire")
    _audit(db, user.org_id, user.id, "answer_rereviewed", f"#{qa.id} {req.verdict}")
    db.commit()
    return {"ok": True}


class ReplaceDoc(BaseModel):
    content: str
    note: str = ""


@router.post("/docs/{doc_id}/replace")
def replace_document(doc_id: int, req: ReplaceDoc, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Replace a document's text. Verified answers that cited it go back for re-review, people who used it
    are told, and the graph links are refreshed."""
    d = _visible_doc(db, user, doc_id)
    if not features.can_manage(user, d):
        raise HTTPException(403, "Only the document's owner or a manager can replace its text")
    if len(req.content.strip()) < 10:
        raise HTTPException(400, "The new text is too short")
    d.content, d.content_changed_at = req.content, _now()
    d.last_reviewed_at, d.last_reviewed_by = _now(), user.id      # the person replacing it vouches for the new text
    db.query(models.Chunk).filter_by(document_id=d.id).delete()
    for i, c in enumerate(retrieval.chunk_text(req.content)):
        db.add(models.Chunk(document_id=d.id, org_id=d.org_id, text=c, order_index=i))
    marked = invalidate_answers(db, user, doc_ids=[d.id], reason=f"'{d.title}' was replaced on {_now():%d %b %Y}" + (f" ({req.note})" if req.note else ""))
    told = recall(db, user, doc_ids=[d.id], text=f"the document '{d.title}' was replaced after you used it.")
    on_document_verified(db, d, user)
    _audit(db, d.org_id, user.id, "document_replaced", f"'{d.title}' - {marked} verified answers need re-review, {told} people told")
    db.commit()
    invalidate_links(d.org_id)
    return {"ok": True, "answers_marked": marked, "people_notified": told}


# ------------------------------------------- invalidation and recall notices ---

def _served_chain(db, qa):
    ids = {qa.id}
    for r in db.query(models.QARecord).filter_by(org_id=qa.org_id, served_from_qa_id=qa.id):
        ids.add(r.id)
    return ids


def invalidate_answers(db, actor, doc_ids=(), fact_ids=(), reason="") -> int:
    """Verified answers whose source changed are marked 'needs re-review' (they are NOT deleted)."""
    n = 0
    for qa in db.query(models.QARecord).filter(models.QARecord.org_id == actor.org_id,
                                               models.QARecord.status.in_(["verified", "corrected"])):
        docs, facts = _sources(qa.sources)
        if (docs & set(doc_ids)) or (facts & set(fact_ids)):
            qa.needs_rereview, qa.rereview_reason = True, reason
            n += 1
            for uid in {qa.reviewer_id} | {u.id for u in features._approvers(db, qa.org_id, qa.department)}:
                _notify(db, qa.org_id, actor.id, uid, "rereview",
                        f"Verified answer needs re-review: \"{qa.question[:80]}\" - {reason}", qa_id=qa.id)
    return n


def recall(db, actor, qa_ids=(), fact_ids=(), doc_ids=(), text="", old="", new="") -> int:
    """Tell everyone whose 'I used this' event touched the old answer/fact/document."""
    qa_ids, fact_ids, doc_ids = set(qa_ids), set(fact_ids), set(doc_ids)
    hit = {}
    for e in db.query(models.UsageEvent).filter_by(org_id=actor.org_id):
        try:
            ef = set(json.loads(e.fact_ids or "[]"))
        except Exception:
            ef = set()
        if (e.qa_id in qa_ids) or (ef & fact_ids) or (e.document_id in doc_ids):
            hit.setdefault(e.user_id, e)
    detail = ""
    if old or new:
        detail = f" Was: {old[:120]}" + (f" Now: {new[:120]}" if new else "")
    for uid, e in hit.items():
        if uid == actor.id:
            continue
        _notify(db, actor.org_id, actor.id, uid, "recall",
                f"Recall: {text}{detail} You used it on {e.created_at:%d %b}" + (f" for \"{e.note[:70]}\"" if e.note else "") +
                ". Please re-check that work.", document_id=e.document_id, qa_id=e.qa_id)
    if hit:
        _audit(db, actor.org_id, actor.id, "recall_sent", f"{len(hit)} people: {text[:120]}")
    return len([u for u in hit if u != actor.id])


def record_usage(db, user, qa_id, document_id, note):
    """Every 'I used this' click becomes an event tied to one specific answer."""
    qa = db.query(models.QARecord).get(qa_id) if qa_id else None
    if qa and (qa.org_id != user.org_id or qa.user_id != user.id):
        qa = None
    facts = sorted(_sources(qa.sources)[1]) if qa else []
    db.add(models.UsageEvent(org_id=user.org_id, user_id=user.id, qa_id=qa.id if qa else None,
                             document_id=document_id, fact_ids=json.dumps(facts), note=(note or "")[:300]))


def on_review_resolved(db, actor, qa, verdict):
    if verdict in ("correct", "reject"):
        label = "corrected" if verdict == "correct" else "marked wrong"
        recall(db, actor, qa_ids=_served_chain(db, qa), text=f"the answer to \"{qa.question}\" was {label}.",
               old=qa.answer_text or "", new=qa.corrected_answer or "")


def on_conflict_resolved(db, actor, keep, drop):
    """A fact just stopped being true: stamp it, mark verified answers built on it, recall the people who used it."""
    drop.superseded_at = _now()
    label = f"'{drop.subject} {drop.relation} {drop.object}'"
    n = invalidate_answers(db, actor, fact_ids=[drop.id], reason=f"the fact {label} was replaced by '{keep.object}'")
    told = recall(db, actor, fact_ids=[drop.id], text=f"the fact {label} is no longer current.", old=drop.object, new=keep.object)
    _audit(db, actor.org_id, actor.id, "fact_superseded", f"{label} -> '{keep.object}'; {n} answers to re-review; {told} people told")


# -------------------------------------------------- escalating review chain ---

def on_flag(db, qa):
    qa.flagged_at, qa.esc_level, qa.esc_due = _now(), 0, _now() + datetime.timedelta(hours=ESC_HOURS)


def run_escalations(db, org_id, force=False):
    """If a flag or review request sits past its deadline it moves up one level:
    supervisor -> department manager -> admin/owner. Every hop is written to the audit log."""
    now = _now() + (datetime.timedelta(days=3650) if force else datetime.timedelta())
    users = _users(db, org_id)
    hops = []
    nxt_due = _now() + datetime.timedelta(hours=ESC_HOURS)

    for qa in db.query(models.QARecord).filter_by(org_id=org_id, status="flagged"):
        if qa.esc_due is None:
            qa.flagged_at = qa.flagged_at or qa.created_at
            qa.esc_due = nxt_due if not force else now - datetime.timedelta(seconds=1)
        if qa.esc_due > now:
            continue
        asker = users.get(qa.user_id)
        if not asker:
            continue
        chain = _build_chain(db, org_id, [_supervisor_id(db, asker)], qa.department, {asker.id})
        lvl = (qa.esc_level or 0) + 1
        if lvl >= len(chain):
            continue
        frm, to = users.get(chain[lvl - 1]), users.get(chain[lvl])
        qa.esc_level, qa.esc_due = lvl, nxt_due
        _notify(db, org_id, asker.id, to.id, "review_escalated",
                f"Escalated to you: {asker.name}'s flagged answer \"{qa.question[:80]}\" was not reviewed by {frm.name if frm else 'the previous reviewer'} in time.", qa_id=qa.id)
        _audit(db, org_id, asker.id, "review_escalated", f"flagged answer #{qa.id}: level {lvl-1} {frm.name if frm else '?'} -> level {lvl} {to.name}")
        hops.append({"kind": "answer", "id": qa.id, "from": frm.name if frm else None, "to": to.name, "level": lvl})

    for r in db.query(models.DocReviewRequest).filter(models.DocReviewRequest.org_id == org_id,
                                                      models.DocReviewRequest.status.in_(["open", "assigned"])):
        if r.esc_due is None:
            r.esc_due = nxt_due
        if r.esc_due > now:
            continue
        d = db.query(models.Document).get(r.document_id)
        if not d:
            continue
        first = [d.owner_id] if r.status == "open" else [r.assignee_id, d.owner_id]
        chain = _build_chain(db, org_id, first, d.department, {r.requested_by} if r.status == "open" else set())
        lvl = (r.esc_level or 0) + 1
        if lvl >= len(chain):
            continue
        frm, to = users.get(chain[lvl - 1]), users.get(chain[lvl])
        r.esc_level, r.esc_due, r.holder_id = lvl, nxt_due, to.id
        _notify(db, org_id, r.requested_by if r.requested_by != to.id else (frm.id if frm else to.id), to.id, "review_escalated",
                f"Escalated to you: the review of '{d.title}' was not done by {frm.name if frm else 'the previous person'} in time.", document_id=d.id)
        _audit(db, org_id, r.requested_by, "review_escalated", f"review of '{d.title}': level {lvl-1} {frm.name if frm else '?'} -> level {lvl} {to.name}")
        hops.append({"kind": "document", "id": d.id, "from": frm.name if frm else None, "to": to.name, "level": lvl})
    db.commit()
    return hops


def run_all_escalations():
    db = SessionLocal()
    try:
        for (oid,) in db.query(models.Organization.id).all():
            run_escalations(db, oid)
    except Exception as e:
        print("escalation check skipped:", e)
    finally:
        db.close()


@router.get("/escalations")
def escalations(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    run_escalations(db, user.org_id)
    users = _users(db, user.org_id)
    items = []
    for qa in db.query(models.QARecord).filter_by(org_id=user.org_id, status="flagged"):
        if not retrieval.dept_ok(user, qa.department):
            continue
        asker = users.get(qa.user_id)
        chain = _build_chain(db, user.org_id, [_supervisor_id(db, asker)], qa.department, {asker.id}) if asker else []
        lvl = min(qa.esc_level or 0, max(len(chain) - 1, 0))
        items.append({"kind": "answer", "id": qa.id, "title": qa.question, "level": lvl,
                      "holder": users[chain[lvl]].name if chain else None, "due": _iso(qa.esc_due),
                      "chain": [users[i].name for i in chain]})
    for r in db.query(models.DocReviewRequest).filter(models.DocReviewRequest.org_id == user.org_id,
                                                      models.DocReviewRequest.status.in_(["open", "assigned"])):
        d = db.query(models.Document).get(r.document_id)
        if not d or not retrieval.user_can_see_document(user, d, db):
            continue
        h = users.get(r.holder_id)
        items.append({"kind": "document", "id": d.id, "title": d.title, "level": r.esc_level or 0,
                      "holder": h.name if h else None, "due": _iso(r.esc_due), "status": r.status,
                      "chain": []})
    log = [{"at": _iso(a.created_at), "detail": a.detail} for a in
           db.query(models.AuditLog).filter_by(org_id=user.org_id, action="review_escalated")
           .order_by(models.AuditLog.created_at.desc()).limit(30)]
    return {"hours": ESC_HOURS, "items": items, "log": log}


class RunEsc(BaseModel):
    force: bool = False


@router.post("/escalations/run")
def escalations_run(req: RunEsc, user: models.User = Depends(auth.require_role("admin")), db: Session = Depends(get_db)):
    """force=true pretends every deadline has passed - handy for a demo."""
    return {"hops": run_escalations(db, user.org_id, force=req.force)}


# ---------------------------------------- questions log, gaps, last known truth ---

def log_question(db, user, qa, answered, matched_verified, conf):
    """Phase 0 question log + automatic routing of repeated gaps."""
    db.add(models.QuestionLog(org_id=user.org_id, user_id=user.id, department=user.department, question=qa.question,
                              qa_id=qa.id, answered=bool(answered), matched_verified=bool(matched_verified),
                              confidence=(conf or {}).get("level") if isinstance(conf, dict) else None))
    db.commit()
    try:
        if not matched_verified and (not answered or (isinstance(conf, dict) and conf.get("level") == "Low")):
            cl = _gap_clusters(db, user.org_id, user.department)
            mine = _tok(qa.question)
            for c in cl:
                if any(_dice(mine, t) >= 0.99 for t in c["toks"]) and len(c["rows"]) >= GAP_AUTO and not c["answered"] and not c["route"]:
                    _route_gap(db, user.org_id, user.department, c["rep"], len(c["rows"]), user.id)
                    db.commit()
                    break
    except Exception as e:      # gap routing must never break Ask
        print("gap routing skipped:", e)


def backfill_question_log(db, org_id):
    if db.query(models.QuestionLog).filter_by(org_id=org_id).count():
        return
    for qa in db.query(models.QARecord).filter_by(org_id=org_id):
        db.add(models.QuestionLog(org_id=org_id, user_id=qa.user_id, department=qa.department, question=qa.question,
                                  qa_id=qa.id, answered=bool(qa.answer_text), matched_verified=qa.served_from_qa_id is not None,
                                  confidence=None, created_at=qa.created_at))
    db.commit()


def _gap_clusters(db, org_id, dept=None, days=90):
    backfill_question_log(db, org_id)
    since = _now() - datetime.timedelta(days=days)
    q = db.query(models.QuestionLog).filter(models.QuestionLog.org_id == org_id, models.QuestionLog.created_at >= since,
                                            models.QuestionLog.matched_verified == False)  # noqa: E712
    if dept:
        q = q.filter(models.QuestionLog.department == dept)
    bad_qa = {qa.id for qa in db.query(models.QARecord).filter(models.QARecord.org_id == org_id,
                                                              models.QARecord.status.in_(["flagged", "rejected"]))}
    # a gap = no verified answer AND (nothing found, low confidence, or a person flagged the answer as wrong)
    rows = [r for r in q.order_by(models.QuestionLog.created_at).limit(500)
            if (not r.answered) or r.confidence == "Low" or r.qa_id in bad_qa]
    clusters = []
    for r in rows:
        t = _tok(r.question)
        best, bs = None, 0.0
        for c in clusters:
            if c["dept"] != r.department:
                continue
            s = max(_dice(t, x) for x in c["toks"])
            if s > bs:
                best, bs = c, s
        if best and bs >= 0.55:
            best["rows"].append(r); best["toks"].append(t)
        else:
            clusters.append({"rep": r.question, "dept": r.department, "rows": [r], "toks": [t]})
    verified = [(rec, _tok(rec.question)) for rec in db.query(models.QARecord).filter(
        models.QARecord.org_id == org_id, models.QARecord.status.in_(["verified", "corrected"]))]
    routes = db.query(models.GapRoute).filter_by(org_id=org_id).all()
    for c in clusters:
        c["answered"] = any(_dice(t, vt) >= 0.5 and (not rec.department or rec.department == c["dept"] or retrieval.dept_ok(
            SimpleNamespace(role="member", department=c["dept"], id=-1, org_id=org_id), rec.department)) for rec, vt in verified for t in c["toks"][:3])
        c["route"] = next((g for g in routes if any(_dice(_tok(g.question), t) >= 0.55 for t in c["toks"][:3]) and g.department == c["dept"]), None)
    return clusters


def _route_gap(db, org_id, dept, question, count, actor_id):
    heads = features._approvers(db, org_id, dept)
    head = heads[0] if heads else None
    task = models.Task(org_id=org_id, title=f"Write a verified answer: \"{question}\" (asked {count} times, no verified answer)",
                       owner=head.name if head else None, assignee_id=head.id if head else None, created_by=actor_id)
    db.add(task)
    db.flush()
    g = models.GapRoute(org_id=org_id, department=dept, question=question, count_at_route=count,
                        routed_to=head.id if head else None, task_id=task.id)
    db.add(g)
    if head:
        _notify(db, org_id, actor_id, head.id, "knowledge_gap",
                f"Knowledge gap in {dept}: \"{question}\" was asked {count} times with no verified answer. Please write the answer (Insights -> Knowledge gaps).")
    _audit(db, org_id, actor_id, "gap_routed", f"{dept}: \"{question}\" x{count} -> {head.name if head else 'nobody'}")
    return g


def _scope_dept(user, dept):
    return auth.rank(user) >= ADMIN or dept == user.department


@router.get("/gaps")
def gaps(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    out = []
    users = _users(db, user.org_id)
    for c in _gap_clusters(db, user.org_id):
        if len(c["rows"]) < GAP_MIN or not _scope_dept(user, c["dept"]):
            continue
        g = c["route"]
        out.append({"question": c["rep"], "department": c["dept"], "count": len(c["rows"]),
                    "askers": len({r.user_id for r in c["rows"]}), "last_asked": _iso(c["rows"][-1].created_at),
                    "answered": bool(c["answered"] or (g and g.answered_at)),
                    "routed_to": users[g.routed_to].name if g and g.routed_to in users else None,
                    "routed": bool(g), "examples": [r.question for r in c["rows"][:4]]})
    out.sort(key=lambda r: (r["answered"], -r["count"]))
    return out


class GapIn(BaseModel):
    question: str
    department: str
    answer: str = ""


@router.post("/gaps/route")
def gap_route(req: GapIn, user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    if not _scope_dept(user, req.department):
        raise HTTPException(403, "That gap belongs to another department")
    for c in _gap_clusters(db, user.org_id, req.department):
        if _dice(_tok(req.question), _tok(c["rep"])) >= 0.9:
            if c["route"] and not c["route"].answered_at:
                return {"ok": True, "message": "Already sent to the department manager."}
            g = _route_gap(db, user.org_id, req.department, c["rep"], len(c["rows"]), user.id)
            db.commit()
            return {"ok": True, "message": f"Sent to {db.query(models.User).get(g.routed_to).name if g.routed_to else 'nobody (no manager found)'}."}
    raise HTTPException(404, "Gap not found")


@router.post("/gaps/answer")
def gap_answer(req: GapIn, user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    """The manager writes the verified answer. It is served first on the next matching question."""
    if not _scope_dept(user, req.department):
        raise HTTPException(403, "That gap belongs to another department")
    if not req.answer.strip():
        raise HTTPException(400, "Write the answer")
    clusters = [c for c in _gap_clusters(db, user.org_id, req.department) if _dice(_tok(req.question), _tok(c["rep"])) >= 0.9]
    qa = models.QARecord(org_id=user.org_id, user_id=user.id, department=req.department, question=req.question.strip(),
                         answer_text=req.answer.strip(), corrected_answer=req.answer.strip(), status="corrected",
                         reviewer_id=user.id, reviewed_at=_now(), review_note="Written to close a knowledge gap", sources="[]")
    db.add(qa)
    db.flush()
    askers = set()
    if clusters:
        askers = {r.user_id for r in clusters[0]["rows"]}
        g = clusters[0]["route"]
        if g:
            g.answered_at, g.answered_qa_id = _now(), qa.id
            t = db.query(models.Task).get(g.task_id) if g.task_id else None
            if t:
                t.status = "done"
    for uid in askers:
        _notify(db, user.org_id, user.id, uid, "gap_closed", f"A verified answer now exists for \"{req.question}\". Ask again to see it.", qa_id=qa.id)
    _audit(db, user.org_id, user.id, "gap_answered", f"{req.department}: \"{req.question}\"")
    db.commit()
    return {"ok": True, "qa_id": qa.id, "askers_told": len(askers - {user.id})}


def last_known_truth(db, user, question):
    """When there's no current answer, show the last thing that WAS verified - clearly labelled as history."""
    words = {w for w in nlp.expand_words(question) if len(w) >= 4}
    cands = []      # (when, payload)
    if words:
        for f in db.query(models.Fact).filter(models.Fact.org_id == user.org_id, models.Fact.status == "superseded"):
            hay = set(nlp.expand_words(f"{f.subject} {f.relation} {f.object}"))
            if len(words & hay) >= 2:
                when = f.superseded_at or f.created_at
                cands.append((when, {"text": f"{f.subject} {f.relation.replace('_', ' ')} {f.object}", "as_of": _iso(f.created_at),
                                     "ended": _iso(f.superseded_at), "source": "knowledge graph"}))
    qt = _tok(question)
    for rec in db.query(models.QARecord).filter(models.QARecord.org_id == user.org_id,
                                                models.QARecord.status.in_(["verified", "corrected"])):
        if not retrieval.dept_ok(user, rec.department) or retrieval._cites_private_doc(db, rec):
            continue
        if _dice(qt, _tok(rec.question)) >= 0.45:
            when = rec.reviewed_at or rec.created_at
            cands.append((when, {"text": rec.corrected_answer or rec.answer_text, "as_of": _iso(when), "ended": None,
                                 "source": "verified answer" + (" (waiting for re-review)" if rec.needs_rereview else "")}))
    return max(cands, key=lambda c: c[0])[1] if cands else None


@router.get("/last-known-truth")
def lkt(q: str, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return {"current": "unknown", "last_verified": last_known_truth(db, user, q)}


# ------------------------------------ request access instead of a dead end ---

def _hidden_doc(db, user, question, own_score=0.0):
    """A document that would answer this question better than anything the user can open.
    Only the server ever sees which one - the user is never told its title, department or text."""
    ghost = SimpleNamespace(id=-1, org_id=user.org_id, role="owner", department="", name="", active=True)
    res = retrieval.search_grouped(db, ghost, question)
    ans = res.get("answer")
    if not ans:
        return None
    d = db.query(models.Document).get(ans["document_id"])
    if not d or d.deleted_at is not None or d.visibility == "restricted" or (d.workspace or "company") != "company":
        return None       # restricted documents never even hint that they exist
    if retrieval.user_can_see_document(user, d, db):
        return None
    top = (res.get("documents") or [{}])[0].get("score", 0) or 0
    if own_score and top < own_score * 1.25:
        return None       # what the user can already open is about as good
    return d


def ask_extras(db, user, question, answer_text, verified, res=None):
    extra = {}
    if not answer_text:
        extra["last_known_truth"] = last_known_truth(db, user, question)
    if verified is None:
        own = ((res or {}).get("documents") or [{}])[0].get("score", 0) or 0
        try:
            d = _hidden_doc(db, user, question, own)
        except Exception:
            d = None
        if d:
            pend = db.query(models.DocAccessRequest).filter_by(document_id=d.id, requester_id=user.id, status="pending").first()
            extra["access_hint"] = {"pending": bool(pend), "weak_answer": bool(answer_text)}
    return extra


class AccessQ(BaseModel):
    qa_id: int
    reason: str = ""
    days: int = 1


@router.post("/access-request")
def access_request(req: AccessQ, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Send an access request to the owner of the document that would answer this question.
    The asker never learns the title, department or content - only that a request was sent."""
    generic = {"ok": True, "message": "Request sent to the person who can decide. You'll be notified; nothing about the content was shared."}
    qa = db.query(models.QARecord).get(req.qa_id)
    if not qa or qa.user_id != user.id or req.days not in (1, 7, 30):
        raise HTTPException(404, "Question not found")
    own = (retrieval.search_grouped(db, user, qa.question).get("documents") or [{}])[0].get("score", 0) or 0
    d = _hidden_doc(db, user, qa.question, own)
    if not d:
        return generic
    if not db.query(models.DocAccessRequest).filter_by(document_id=d.id, requester_id=user.id, status="pending").first():
        db.add(models.DocAccessRequest(org_id=user.org_id, document_id=d.id, requester_id=user.id,
                                       reason=(req.reason.strip() or f"Needed to answer: {qa.question}")[:500], days=req.days))
        for t in {d.owner_id} | {u.id for u in features._approvers(db, user.org_id, d.department)}:
            _notify(db, user.org_id, user.id, t, "access_request",
                    f"{user.name} asked \"{qa.question[:100]}\" and needs {req.days}-day access to '{d.title}' to get an answer.", document_id=d.id)
        _audit(db, user.org_id, user.id, "doc_access_requested", f"doc #{d.id} via question #{qa.id}")
        db.commit()
    return generic


# --------------------------------------------------- impact + what-if ---

def _live_facts(db, org_id):
    return db.query(models.Fact).filter(models.Fact.org_id == org_id, models.Fact.status != "superseded").all()


def impact_from(db, user, seed_facts, entities=(), depth=2):
    """Walk the graph outward from a changed fact (or named entities). Only documents the user can open are listed."""
    ensure_links(db, user.org_id)
    facts = _live_facts(db, user.org_id)
    touched = {f.id: 0 for f in seed_facts}
    labels = {x.strip().lower() for f in seed_facts for x in (f.subject, f.object)} | {e.strip().lower() for e in entities if e.strip()}
    seen, frontier = set(labels), set(labels)
    by_id = {f.id: f for f in facts}
    for f in seed_facts:
        by_id.setdefault(f.id, f)
    for lvl in range(1, depth + 1):
        nxt = set()
        for f in facts:
            if f.id in touched:
                continue
            a, b = f.subject.strip().lower(), f.object.strip().lower()
            if a in frontier or b in frontier:
                touched[f.id] = lvl
                nxt |= {a, b} - seen
        seen |= nxt
        frontier = nxt
        if not nxt:
            break
    docs_by_id = {d.id: d for d in _company_docs(db, user.org_id)}
    doc_ids = {l.document_id for l in db.query(models.FactDocLink).filter(models.FactDocLink.fact_id.in_(list(touched) or [0]))}
    for e in entities:
        el = e.strip().lower()
        if len(el) >= 4:
            doc_ids |= {d.id for d in docs_by_id.values() if el in (d.content or "").lower()}
    vis = [docs_by_id[i] for i in doc_ids if i in docs_by_id and retrieval.user_can_see_document(user, docs_by_id[i], db)]
    users = _users(db, user.org_id)
    teams = {d.department for d in vis if d.department and d.department != "All"}
    for d in vis:           # a company-wide document still belongs to its owner's team
        o = users.get(d.owner_id)
        if d.department == "All" and o and o.department:
            teams.add(o.department)
    used = set()
    for e in db.query(models.UsageEvent).filter_by(org_id=user.org_id):
        try:
            ef = set(json.loads(e.fact_ids or "[]"))
        except Exception:
            ef = set()
        if (ef & set(touched)) or e.document_id in {d.id for d in vis}:
            used.add(e.user_id)
    for uid in used:
        if uid in users and users[uid].department:
            teams.add(users[uid].department)
    company_wide = any(d.department == "All" for d in vis)
    return {"summary": f"Affects {len(vis)} document{'s' if len(vis) != 1 else ''} and {len(teams)} team{'s' if len(teams) != 1 else ''}"
                       + (" (plus company-wide documents)" if company_wide and teams else ""),
            "documents": [{"id": d.id, "title": d.title, "department": d.department} for d in sorted(vis, key=lambda x: x.title)],
            "teams": sorted(teams), "people_who_used_it": len(used),
            "facts": [{"id": fid, "text": f"{by_id[fid].subject} {by_id[fid].relation.replace('_', ' ')} {by_id[fid].object}",
                       "distance": lvl} for fid, lvl in sorted(touched.items(), key=lambda x: x[1]) if fid in by_id]}


@router.get("/fact-list")
def fact_list(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return [{"id": f.id, "text": f"{f.subject} {f.relation.replace('_', ' ')} {f.object}", "status": f.status,
             "created_at": _iso(f.created_at), "superseded_at": _iso(f.superseded_at)}
            for f in db.query(models.Fact).filter_by(org_id=user.org_id).order_by(models.Fact.id)]


@router.get("/impact/fact/{fact_id}")
def impact_fact(fact_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.Fact).get(fact_id)
    if not f or f.org_id != user.org_id:
        raise HTTPException(404, "Fact not found")
    r = impact_from(db, user, [f])
    r["fact"] = f"{f.subject} {f.relation.replace('_', ' ')} {f.object}"
    return r


class WhatIf(BaseModel):
    subject: str = ""
    relation: str = ""
    object: str = ""
    text: str = ""


@router.post("/what-if")
def what_if(req: WhatIf, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Preview a policy change BEFORE publishing it. Nothing is written."""
    s, r, o = req.subject.strip().lower(), req.relation.strip().lower(), req.object.strip()
    if not (s and r and o) and not req.text.strip():
        raise HTTPException(400, "Fill subject, relation and new value - or paste the draft policy text")
    live = _live_facts(db, user.org_id)
    same = [f for f in live if s and f.subject.strip().lower() == s and f.relation.strip().lower() == r]
    would_conflict = [f for f in same if f.object.strip().lower() != o.lower()]
    entities = [x for x in (req.subject, req.object) if x.strip()]
    low = req.text.lower()
    for f in live:
        for lab in (f.subject, f.object):
            if len(lab.strip()) >= 4 and lab.strip().lower() in low:
                entities.append(lab)
    seeds = would_conflict or same
    res = impact_from(db, user, seeds, entities)
    res["would_replace"] = [f"{f.subject} {f.relation.replace('_', ' ')} {f.object}" for f in would_conflict]
    res["conflict"] = bool(would_conflict)
    verified = 0
    for qa in db.query(models.QARecord).filter(models.QARecord.org_id == user.org_id, models.QARecord.status.in_(["verified", "corrected"])):
        docs, facts = _sources(qa.sources)
        if facts & {f.id for f in seeds} or docs & {d["id"] for d in res["documents"]}:
            verified += 1
    res["verified_answers_to_recheck"] = verified
    res["note"] = ("Publishing this would flag a conflict for a manager to resolve." if would_conflict
                   else "No existing fact is contradicted; these are the places that mention the same things.")
    return res


@router.get("/blast-radius")
def blast_radius(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    """Rank conflicts and gaps by (dependent documents x how often people ask) so managers fix the worst first."""
    backfill_question_log(db, user.org_id)
    since = _now() - datetime.timedelta(days=90)
    logs = db.query(models.QuestionLog).filter(models.QuestionLog.org_id == user.org_id, models.QuestionLog.created_at >= since).all()
    ltoks = [(l, set(nlp.expand_words(l.question))) for l in logs]
    items = []
    facts = {f.id: f for f in db.query(models.Fact).filter_by(org_id=user.org_id)}
    for c in db.query(models.Conflict).filter_by(org_id=user.org_id, resolved=False):
        o, n = facts.get(c.old_fact_id), facts.get(c.new_fact_id)
        if not o or not n:
            continue
        imp = impact_from(db, user, [o, n], depth=1)
        keys = {w for w in nlp.expand_words(f"{o.subject} {o.relation}") if len(w) >= 4}
        asks = sum(1 for l, t in ltoks if len(keys & t) >= 2)
        docs = len(imp["documents"])
        items.append({"kind": "conflict", "id": c.id, "title": f"{o.subject} {o.relation.replace('_', ' ')}: '{o.object}' vs '{n.object}'",
                      "documents": docs, "asks": asks, "score": max(docs, 1) * max(asks, 1), "teams": imp["teams"]})
    docs_all = _company_docs(db, user.org_id)
    for c in _gap_clusters(db, user.org_id):
        if len(c["rows"]) < GAP_MIN or c["answered"] or not _scope_dept(user, c["dept"]):
            continue
        keys = {w for w in nlp.expand_words(c["rep"]) if len(w) >= 4}
        related = [d for d in docs_all if retrieval.user_can_see_document(user, d, db) and len(keys & set(nlp.expand_words((d.content or "")[:4000]))) >= 2]
        items.append({"kind": "gap", "id": None, "title": c["rep"], "documents": len(related), "asks": len(c["rows"]),
                      "score": max(len(related), 1) * len(c["rows"]), "teams": [c["dept"]], "department": c["dept"]})
    items.sort(key=lambda i: -i["score"])
    return items


# ------------------------------------------------ expertise + bus factor ---

def _topics(d):
    t = {x.strip().lower() for x in (d.tags or "").split(",") if x.strip()}
    if d.department and d.department != "All":
        t.add(d.department.lower())
    return t or {"general"}


def expertise_scores(db, org_id):
    """topic -> user -> {score, last, why}. Real activity only (uploads, reviews, approvals, corrected answers),
    each weighted and halved every HALF_LIFE days."""
    now = _now()
    docs = {d.id: d for d in _company_docs(db, org_id)}
    sc = {}

    def bump(topic, uid, at, w, why):
        if not uid or not at:
            return
        age = max(0.0, (now - at).total_seconds() / 86400)
        e = sc.setdefault(topic, {}).setdefault(uid, {"score": 0.0, "last": at, "why": {}})
        e["score"] += w * (0.5 ** (age / HALF_LIFE))
        e["last"] = max(e["last"], at)
        e["why"][why] = e["why"].get(why, 0) + 1

    for d in docs.values():
        for t in _topics(d):
            bump(t, d.uploaded_by, d.created_at, 3, "uploads")
            if d.last_reviewed_by and d.last_reviewed_at and abs((d.last_reviewed_at - d.created_at).total_seconds()) > 60:
                bump(t, d.last_reviewed_by, d.last_reviewed_at, 2, "reviews")
    for qa in db.query(models.QARecord).filter(models.QARecord.org_id == org_id, models.QARecord.reviewer_id.isnot(None),
                                               models.QARecord.reviewed_at.isnot(None)):
        topics = set()
        for did in _sources(qa.sources)[0]:
            if did in docs:
                topics |= _topics(docs[did])
        topics = topics or {(qa.department or "general").lower()}
        for t in topics:
            bump(t, qa.reviewer_id, qa.reviewed_at, 3 if qa.status == "corrected" else 2, "answers" if qa.status == "corrected" else "approvals")
    for f in db.query(models.Fact).filter(models.Fact.org_id == org_id, models.Fact.created_by.isnot(None), models.Fact.document_id.isnot(None)):
        if f.document_id in docs:
            for t in _topics(docs[f.document_id]):
                bump(t, f.created_by, f.created_at, 1, "facts")
    return sc


@router.get("/expertise")
def expertise(topic: str = "", user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    sc = expertise_scores(db, user.org_id)
    users = _users(db, user.org_id)
    show = auth.rank(user) >= MANAGER

    def rows(t):
        out = []
        for uid, e in sorted(sc.get(t, {}).items(), key=lambda x: -x[1]["score"]):
            u = users.get(uid)
            if u and u.active:
                out.append({"name": u.name, "role": u.role, "department": u.department, "current": e["score"] >= EXPERT_MIN,
                            "score": round(e["score"], 2) if show else None, "last_active": _iso(e["last"]),
                            "activity": ", ".join(f"{n} {k}" for k, n in e["why"].items())})
        return out
    if topic.strip():
        return {"topic": topic.strip().lower(), "people": rows(topic.strip().lower())}
    return [{"topic": t, "people": rows(t)[:3]} for t in sorted(sc)]


@router.get("/bus-factor")
def bus_factor(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    """Topics that depend on ONE person's current expertise (or on nobody's)."""
    sc = expertise_scores(db, user.org_id)
    users = _users(db, user.org_id)
    docs = _company_docs(db, user.org_id)
    out = []
    for t, per in sc.items():
        ndocs = sum(1 for d in docs if t in _topics(d))
        if not ndocs:
            continue
        cur = [(uid, e) for uid, e in per.items() if e["score"] >= EXPERT_MIN and uid in users and users[uid].active]
        if len(cur) == 1:
            uid, e = cur[0]
            out.append({"topic": t, "risk": "single", "documents": ndocs, "who": users[uid].name, "score": round(e["score"], 2),
                        "detail": f"Only {users[uid].name} has current expertise ({ndocs} document{'s' if ndocs != 1 else ''})."})
        elif not cur:
            last = max(per.items(), key=lambda x: x[1]["last"])
            days = (_now() - last[1]["last"]).days
            nm = users[last[0]].name if last[0] in users else "someone who has left"
            out.append({"topic": t, "risk": "none", "documents": ndocs, "who": None, "score": 0,
                        "detail": f"Nobody has current expertise. Last active: {nm}, {days} days ago."})
    out.sort(key=lambda r: (r["risk"] != "none", -r["documents"]))
    return out


# --------------------------------------------------- memory diff ---

def _parse_day(s, default):
    try:
        return datetime.datetime.fromisoformat(s[:10]) if s else default
    except Exception:
        raise HTTPException(400, "Dates look like 2026-09-01")


@router.get("/memory-diff")
def memory_diff(frm: Optional[str] = None, to: Optional[str] = None, user: models.User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    """What the organization knew on date A versus date B (facts are org-wide by design)."""
    a = _parse_day(frm, _now() - datetime.timedelta(days=30))
    b = _parse_day(to, _now()) + (datetime.timedelta(days=1) if to else datetime.timedelta())
    if b < a:
        a, b = b, a
    facts = db.query(models.Fact).filter_by(org_id=user.org_id).all()

    def active_at(f, t):
        return f.created_at <= t and (f.superseded_at is None or f.superseded_at > t)

    def fx(f):
        return {"id": f.id, "text": f"{f.subject} {f.relation.replace('_', ' ')} {f.object}"}
    added = [fx(f) for f in facts if f.created_at > a and f.created_at <= b]
    superseded = [dict(fx(f), at=_iso(f.superseded_at)) for f in facts if f.superseded_at and a < f.superseded_at <= b]
    resolved = db.query(models.AuditLog).filter(models.AuditLog.org_id == user.org_id, models.AuditLog.action == "conflict_resolved",
                                                models.AuditLog.created_at > a, models.AuditLog.created_at <= b).count()
    backfill_question_log(db, user.org_id)
    gap_rows = [r for r in db.query(models.QuestionLog).filter(models.QuestionLog.org_id == user.org_id,
                                                               models.QuestionLog.created_at > a, models.QuestionLog.created_at <= b,
                                                               models.QuestionLog.matched_verified == False) if not r.answered or r.confidence == "Low"]  # noqa: E712
    opened = len({" ".join(sorted(_tok(r.question))) for r in gap_rows})
    closed = db.query(models.GapRoute).filter(models.GapRoute.org_id == user.org_id, models.GapRoute.answered_at > a,
                                              models.GapRoute.answered_at <= b).count()
    return {"from": _iso(a), "to": _iso(b),
            "known_then": sum(1 for f in facts if active_at(f, a)), "known_now": sum(1 for f in facts if active_at(f, b)),
            "added": added, "superseded": superseded, "conflicts_resolved": resolved,
            "gaps_opened": opened, "unanswered_questions": len(gap_rows), "gaps_closed": closed}


# ------------------------------------------------ conflict arbitration ---

@router.get("/conflicts/{conflict_id}/hint")
def conflict_hint(conflict_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Suggests which side to trust (who added it, role, whether its source was verified, recency). A human still decides."""
    c = db.query(models.Conflict).get(conflict_id)
    if not c or c.org_id != user.org_id:
        raise HTTPException(404, "Conflict not found")
    users = _users(db, user.org_id)
    now = _now()
    sides = []
    for fid in (c.old_fact_id, c.new_fact_id):
        f = db.query(models.Fact).get(fid)
        who = users.get(f.created_by)
        d = db.query(models.Document).get(f.document_id) if f.document_id else None
        pts, why = 0.0, []
        if who:
            pts += auth.rank(who)
            why.append(f"added by {who.name} ({who.role})")
        else:
            why.append("no recorded author")
        if d:
            if d.verified_until and d.verified_until >= now:
                pts += 2; why.append(f"source '{d.title}' is verified")
            elif d.last_reviewed_at and (now - d.last_reviewed_at).days < REVIEW_DAYS:
                pts += 1; why.append(f"source '{d.title}' was reviewed {(now - d.last_reviewed_at).days} days ago")
            else:
                why.append(f"source '{d.title}' has not been reviewed recently")
        else:
            why.append("no source document")
        sides.append({"fact_id": f.id, "object": f.object, "points": pts, "why": why, "created_at": f.created_at})
    newer = max(sides, key=lambda s: s["created_at"])
    newer["points"] += 2
    newer["why"].append("more recent")
    for s in sides:
        s["created_at"] = _iso(s["created_at"])
        s["points"] = round(s["points"], 1)
    top = max(sides, key=lambda s: s["points"])
    gap = abs(sides[0]["points"] - sides[1]["points"])
    return {"suggest_fact_id": top["fact_id"] if gap >= 1 else None, "sides": sides,
            "strength": "clear" if gap >= 3 else ("lean" if gap >= 1 else "toss-up"),
            "note": "This is a hint, not a decision. A manager or admin must still resolve the conflict."}
