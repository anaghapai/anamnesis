"""Knowledge Impact - "what does this change touch, and why?"

When a document is edited (or a fact is superseded) Anamnesis works out, deterministically and without
any generative model, which answers, verified answers, graph facts, related documents and people were
built on the OLD wording. Affected answers are marked *needs review* (nothing is ever deleted), their
askers are notified, and a manager can revalidate ("still valid") or replace them (the old answer is
kept as *superseded*). Every flag carries a "why" chain: source -> what changed -> passage -> answer.

Also here: document version history + compare, and the answer state badge (current / verified /
may be outdated / needs review / conflicting / weak / no knowledge).
"""
import re
import json
import difflib
import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from . import models, auth, retrieval, nlp
from .db import get_db

router = APIRouter()
MANAGER = auth.ROLE_RANK["manager"]
ADMIN = auth.ROLE_RANK["admin"]
STALE_DAYS = 90
BANNER_DAYS = 30

_NEW_COLUMNS = {
    "qa_records": [("impact_status", "TEXT"), ("impact_reason", "TEXT"),
                   ("impact_event_id", "INTEGER"), ("superseded_by", "INTEGER")],
}


def ensure_schema(engine):
    """Adds the impact columns to an older anamnesis.db automatically (safe on every start)."""
    with engine.begin() as con:
        for table, cols in _NEW_COLUMNS.items():
            existing = {r[1] for r in con.execute(sql_text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue
            for name, coltype in cols:
                if name not in existing:
                    con.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


# ----------------------------------------------------------------- helpers ---

def _now():
    return datetime.datetime.utcnow()


def _iso(dt):
    return dt.isoformat() if dt else None


def _norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _sources(qa) -> list:
    try:
        return json.loads(qa.sources or "[]")
    except Exception:
        return []


def _names(db: Session, org_id: int) -> dict:
    return {u.id: u.name for u in db.query(models.User).filter_by(org_id=org_id)}


def _notify(db: Session, org_id, from_id, to_id, text, qa_id=None, document_id=None):
    if to_id and to_id != from_id:
        db.add(models.WorkUpdate(org_id=org_id, from_user_id=from_id, to_user_id=to_id, kind="review_result",
                                 text=text, qa_id=qa_id, document_id=document_id))


def _log(db: Session, org_id, user_id, action, detail=""):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action, detail=detail))


def _ws(d) -> str:
    return d.workspace or "company"


def _company_visible(db: Session, user, d: models.Document) -> bool:
    return (d is not None and d.deleted_at is None and _ws(d) == "company"
            and retrieval.user_can_see_document(user, d, db))


def can_edit(user: models.User, d: models.Document) -> bool:
    """Owner / uploader, managers of the document's own department, admins and owners."""
    if d.org_id != user.org_id or _ws(d) != "company" or d.deleted_at is not None:
        return False
    if auth.rank(user) >= ADMIN:
        return True
    if auth.rank(user) >= MANAGER and (user.id in (d.owner_id, d.uploaded_by) or d.department == user.department):
        return True
    return False


def _can_manage(user: models.User, qa: models.QARecord, allow_own=False) -> bool:
    return (auth.rank(user) >= MANAGER and user.org_id == qa.org_id and retrieval.dept_ok(user, qa.department)
            and (allow_own or qa.user_id != user.id or user.role == "owner"))


# --------------------------------------------------------------------- diff ---

def _sents(text: str) -> List[str]:
    out = []
    for line in (text or "").replace("\r", "").split("\n"):
        if line.strip():
            out.extend(retrieval.split_sentences(line))
    return [s.strip() for s in out if s.strip()]


_PUNCT = ".,;:!?()\"'"


def _delta(old: Optional[str], new: Optional[str]):
    """The few words that actually differ, e.g. 90 -> 60."""
    if not old or not new:
        return None
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = [o for o in sm.get_opcodes() if o[0] != "equal"]
    if not ops or len(ops) > 3:
        return None
    frm = " … ".join(" ".join(a[i1:i2]).strip(_PUNCT) for _, i1, i2, _, _ in ops)
    to = " … ".join(" ".join(b[j1:j2]).strip(_PUNCT) for _, _, _, j1, j2 in ops)
    return {"from": frm or "(nothing)", "to": to or "(nothing)"}


def _change(o: Optional[str], n: Optional[str]) -> dict:
    return {"old": o, "new": n, "delta": _delta(o, n)}


def diff_changes(old_text: str, new_text: str) -> List[dict]:
    a, b = _sents(old_text), _sents(new_text)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        olds, news = a[i1:i2], b[j1:j2]
        for k in range(max(len(olds), len(news))):
            out.append(_change(olds[k] if k < len(olds) else None, news[k] if k < len(news) else None))
    return out


def _overlaps(old_sentence: str, blob: str) -> bool:
    o, b = _norm(old_sentence), _norm(blob)
    if not o or not b:
        return False
    if o in b:
        return True
    ow, bw = set(o.split()), set(b.split())
    return len(ow) >= 4 and len(ow & bw) / len(ow) >= 0.9


# ------------------------------------------------------------- flag / state ---

def flag_for(db: Session, qa: models.QARecord) -> Optional[dict]:
    if qa.impact_status == "needs_review":
        return {"status": "needs_review", "reason": qa.impact_reason, "event_id": qa.impact_event_id,
                "flag_qa_id": qa.id}
    if qa.served_from_qa_id:
        o = db.get(models.QARecord, qa.served_from_qa_id)
        if o is not None and o.impact_status == "needs_review":
            return {"status": "needs_review", "reason": o.impact_reason, "event_id": o.impact_event_id,
                    "flag_qa_id": o.id}
    return None


def answer_state(db: Session, answered: bool, verified, flag, documents, hop, conf) -> dict:
    """One plain-language badge per answer."""
    if not answered:
        return {"code": "no_knowledge", "label": "No knowledge found",
                "detail": "Nothing in the knowledge you are allowed to open answers this."}
    if flag:
        return {"code": "needs_review", "label": "Needs review",
                "detail": "A source this answer relied on changed. " + (flag.get("reason") or "")}
    if hop:
        ids = [h.get("fact_id") for h in hop.get("hops", []) if h.get("fact_id")]
        if ids and db.query(models.Fact).filter(models.Fact.id.in_(ids), models.Fact.status == "conflicting").count():
            return {"code": "conflicting", "label": "Sources disagree",
                    "detail": "A fact this answer is built on has an unresolved conflict. Ask a manager to resolve it."}
    if verified:
        who = verified.get("reviewer")
        return {"code": "verified", "label": "Verified by a person",
                "detail": f"Verified by {who}." if who else "A person verified this answer."}
    top = (documents or [None])[0]
    if top:
        d = db.get(models.Document, top.get("document_id"))
        age = top.get("age_days", 0)
        fresh = bool(d and d.verified_until and d.verified_until >= _now())
        if age > STALE_DAYS and not fresh:
            return {"code": "stale", "label": "May be outdated",
                    "detail": f"The source is {age} days old and has not been re-verified."}
    if conf and conf.get("level") == "Low":
        return {"code": "insufficient", "label": "Weak evidence",
                "detail": "The match with the sources is weak. Consider asking someone who knows."}
    return {"code": "current", "label": "From current sources",
            "detail": "Extracted from documents you may open; nothing known to have changed."}


# ----------------------------------------------------------------- analysis ---

def _qa_rows(db: Session, org_id: int):
    return db.query(models.QARecord).filter(
        models.QARecord.org_id == org_id, models.QARecord.answer_text.isnot(None),
        models.QARecord.status != "superseded").all()


def _final_text(qa) -> str:
    return qa.corrected_answer or qa.answer_text or ""


def _is_verified(qa) -> bool:
    return qa.status in ("verified", "corrected")


def _finish(db: Session, actor, analysis: dict, answers: List[dict], facts, docs) -> dict:
    names = _names(db, actor.org_id)
    people = {}
    for a in answers:
        p = people.setdefault(a["asker_id"], {"id": a["asker_id"], "name": names.get(a["asker_id"], "—"), "answers": 0})
        p["answers"] += 1
    actions = []
    if answers:
        v = sum(1 for a in answers if a["verified"])
        actions.append(f"Revalidate {len(answers)} answer(s) — {v} human-verified — in the queue below; nothing is deleted.")
    if facts:
        actions.append(f"Review {len(facts)} graph fact(s) that came from the old wording.")
    if people:
        actions.append(f"{len(people)} person(s) received the old information and will be notified.")
    if docs:
        actions.append("Check related documents for the same wording: " + ", ".join(d["title"] for d in docs[:3]) + ".")
    if not actions:
        actions.append("No answer depends on the old wording yet.")
    analysis.update({
        "answers": answers, "facts": facts, "documents": docs, "people": list(people.values()),
        "counts": {"changes": len(analysis.get("changes", [])), "answers": len(answers),
                   "verified_answers": sum(1 for a in answers if a["verified"]), "facts": len(facts),
                   "documents": len(docs), "people": len(people)},
        "recommended_actions": actions})
    return analysis


def _add_served(rows, answers, names, dept_ok_user=None):
    """Answers that were served from an affected verified answer are affected too."""
    hit = {a["qa_id"]: a for a in answers}
    for qa in rows:
        if qa.id in hit or not qa.served_from_qa_id or qa.served_from_qa_id not in hit:
            continue
        o = hit[qa.served_from_qa_id]
        answers.append({**o, "qa_id": qa.id, "question": qa.question, "status": qa.status, "asker_id": qa.user_id,
                        "asker": names.get(qa.user_id, "—"), "department": qa.department, "via": "verified",
                        "answer": _final_text(qa), "origin_qa_id": o["qa_id"], "verified": _is_verified(qa)})


def analyze_document(db: Session, actor, doc: models.Document, old_text: str, new_text: str, version: int) -> dict:
    changes = diff_changes(old_text, new_text)
    names = _names(db, actor.org_id)
    rows = _qa_rows(db, actor.org_id)
    answers = []
    for qa in rows:
        found = None
        for s in _sources(qa):
            if s.get("document_id") != doc.id:
                continue
            blob = " ".join(x for x in (s.get("match"), s.get("text"), s.get("before"), s.get("after"),
                                        s.get("answer_text")) if x)
            for ch in changes:
                if ch["old"] and _overlaps(ch["old"], blob):
                    found = (ch, s)
                    break
            if found:
                break
        if found:
            ch, s = found
            answers.append({"qa_id": qa.id, "question": qa.question, "status": qa.status, "asker_id": qa.user_id,
                            "asker": names.get(qa.user_id, "—"), "department": qa.department, "via": "passage",
                            "answer": _final_text(qa), "old": ch["old"], "new": ch["new"], "delta": ch["delta"],
                            "passage": s.get("match") or s.get("text"), "origin_qa_id": None,
                            "verified": _is_verified(qa)})
    # facts that were extracted from the old wording
    old_blob = _norm(" ".join(c["old"] for c in changes if c["old"]))
    new_full = _norm(new_text)
    facts = []
    for f in db.query(models.Fact).filter(models.Fact.org_id == actor.org_id, models.Fact.document_id == doc.id,
                                          models.Fact.status != "superseded"):
        o = _norm(f.object)
        if o and o in old_blob and o not in new_full:
            facts.append({"id": f.id, "subject": f.subject, "relation": f.relation, "object": f.object,
                          "status": f.status})
    fact_ids = {f["id"] for f in facts}
    if fact_ids:
        seen = {a["qa_id"] for a in answers}
        for qa in rows:
            if qa.id in seen:
                continue
            for s in _sources(qa):
                if s.get("kind") == "hop" and any(h.get("fact_id") in fact_ids for h in s.get("hops", [])):
                    answers.append({"qa_id": qa.id, "question": qa.question, "status": qa.status,
                                    "asker_id": qa.user_id, "asker": names.get(qa.user_id, "—"),
                                    "department": qa.department, "via": "fact", "answer": _final_text(qa),
                                    "old": None, "new": None, "delta": changes[0]["delta"] if changes else None,
                                    "passage": None, "origin_qa_id": None, "verified": _is_verified(qa)})
                    break
    _add_served(rows, answers, names)
    # related documents: same entities, still the old wording
    labels = {x for f in db.query(models.Fact).filter(models.Fact.org_id == actor.org_id,
                                                      models.Fact.document_id == doc.id,
                                                      models.Fact.status != "superseded")
              for x in (f.subject, f.object)}
    docs = []
    for d in db.query(models.Document).filter(models.Document.org_id == actor.org_id, models.Document.id != doc.id):
        if not _company_visible(db, actor, d):
            continue
        low = d.content.lower()
        via = sorted(l.lower() for l in labels if len(l) >= 4 and l.lower() in low)
        if via:
            docs.append({"id": d.id, "title": d.title, "via": via[:3]})
    analysis = {"kind": "document", "document": {"id": doc.id, "title": doc.title}, "version": version,
                "changes": changes}
    return _finish(db, actor, analysis, answers, facts, docs[:6])


def analyze_fact(db: Session, actor, fact: models.Fact, keep: Optional[models.Fact] = None) -> dict:
    if keep is None:
        c = db.query(models.Conflict).filter(
            (models.Conflict.old_fact_id == fact.id) | (models.Conflict.new_fact_id == fact.id)).first()
        if c:
            other = c.new_fact_id if c.old_fact_id == fact.id else c.old_fact_id
            keep = db.get(models.Fact, other)
    old_txt = f"{fact.subject} {fact.relation} {fact.object}"
    names = _names(db, actor.org_id)
    rows = _qa_rows(db, actor.org_id)
    obj, subj = _norm(fact.object), _norm(fact.subject)
    delta = {"from": fact.object, "to": keep.object} if keep is not None else None
    answers = []
    for qa in rows:
        via = None
        for s in _sources(qa):
            if s.get("kind") == "hop" and any(h.get("fact_id") == fact.id for h in s.get("hops", [])):
                via = "fact"
                break
        if not via and len(obj) >= 3 and subj:
            text = _norm(_final_text(qa))
            if obj in text and (subj in text or subj in _norm(qa.question)):
                via = "fact"
        if via:
            answers.append({"qa_id": qa.id, "question": qa.question, "status": qa.status, "asker_id": qa.user_id,
                            "asker": names.get(qa.user_id, "—"), "department": qa.department, "via": via,
                            "answer": _final_text(qa), "old": None, "new": None, "delta": delta, "passage": None,
                            "origin_qa_id": None, "verified": _is_verified(qa)})
    _add_served(rows, answers, names)
    docs = []
    for d in db.query(models.Document).filter(models.Document.org_id == actor.org_id):
        if not _company_visible(db, actor, d):
            continue
        if d.id == fact.document_id:
            docs.append({"id": d.id, "title": d.title, "via": ["source of this fact"]})
        elif len(subj) >= 4 and subj in _norm(d.content):
            docs.append({"id": d.id, "title": d.title, "via": [fact.subject.lower()]})
    analysis = {"kind": "fact", "fact": {"id": fact.id, "old": old_txt,
                                         "new": (f"{keep.subject} {keep.relation} {keep.object}" if keep else None)},
                "version": None, "changes": []}
    res = _finish(db, actor, analysis, answers, [], docs[:6])
    res["counts"]["facts"] = 1
    return res


def _reason_for(kind: str, doc, version, entry, fact_old=None, fact_new=None) -> str:
    if kind == "document":
        d = entry.get("delta")
        base = f"'{doc.title}' changed (v{version})"
        return base + (f": “{d['from']}” → “{d['to']}”" if d else "")
    if fact_new:
        return f"Fact superseded: “{fact_old}” was replaced by “{fact_new}”"
    return f"Fact superseded: “{fact_old}” is no longer current"


def _apply_flags(db: Session, actor, event: models.ImpactEvent, analysis: dict, reason_of) -> int:
    by_asker = {}
    for a in analysis["answers"]:
        qa = db.get(models.QARecord, a["qa_id"])
        if qa is None:
            continue
        a["reason"] = reason_of(a)
        qa.impact_status, qa.impact_reason, qa.impact_event_id = "needs_review", a["reason"], event.id
        by_asker.setdefault(qa.user_id, []).append(qa)
    for uid, qas in by_asker.items():
        first = qas[0]
        more = f" (+{len(qas) - 1} more)" if len(qas) > 1 else ""
        _notify(db, actor.org_id, actor.id, uid,
                f"Knowledge changed: your answer to “{first.question}”{more} may be outdated. {first.impact_reason}",
                qa_id=first.id, document_id=event.document_id)
    return sum(len(v) for v in by_asker.values())


def on_fact_superseded(db: Session, actor, drop: models.Fact, keep: models.Fact) -> int:
    """Called when a conflict is resolved: answers built on the losing fact -> needs review."""
    analysis = analyze_fact(db, actor, drop, keep)
    ev = models.ImpactEvent(org_id=actor.org_id, kind="fact", fact_id=drop.id,
                            title=f"Fact superseded: {analysis['fact']['old']}", analysis="{}", created_by=actor.id)
    db.add(ev)
    db.flush()
    n = _apply_flags(db, actor, ev, analysis,
                     lambda a: _reason_for("fact", None, None, a, drop.object, keep.object))
    analysis["event_id"] = ev.id
    ev.analysis = json.dumps(analysis)
    _log(db, actor.org_id, actor.id, "impact_fact", f"fact #{drop.id} superseded; {n} answer(s) flagged")
    db.commit()
    return n


# ---------------------------------------------------------------- endpoints ---

class ContentIn(BaseModel):
    content: str
    note: Optional[str] = ""
    preview: bool = False


class RevalidateIn(BaseModel):
    action: str
    note: Optional[str] = None
    new_answer: Optional[str] = None


def _doc_for(db: Session, user, doc_id: int) -> models.Document:
    d = db.get(models.Document, doc_id)
    if not d or d.org_id != user.org_id or d.deleted_at is not None or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    return d


def _version_rows(db: Session, doc_id: int):
    return db.query(models.DocumentVersion).filter_by(document_id=doc_id).order_by(models.DocumentVersion.version).all()


def _current_version(db: Session, doc_id: int) -> int:
    rows = _version_rows(db, doc_id)
    return rows[-1].version if rows else 1


@router.put("/documents/{doc_id}/content")
def edit_content(doc_id: int, req: ContentIn, user: models.User = Depends(auth.get_current_user),
                 db: Session = Depends(get_db)):
    d = _doc_for(db, user, doc_id)
    if not can_edit(user, d):
        raise HTTPException(403, "Only the owner, the department's managers or admins can change this document")
    new = req.content
    if len(new.strip()) < 10:
        raise HTTPException(400, "The document text can't be empty")
    if new.strip() == (d.content or "").strip():
        return {"applied": False, "preview": req.preview, "analysis": None,
                "message": "Nothing changed — the text is identical to the current version."}
    version = _current_version(db, d.id) + 1
    analysis = analyze_document(db, user, d, d.content, new, version)
    if req.preview:
        return {"applied": False, "preview": True, "analysis": analysis}

    if not _version_rows(db, d.id):                       # first managed edit: keep the original as v1
        db.add(models.DocumentVersion(org_id=d.org_id, document_id=d.id, version=1, title=d.title, content=d.content,
                                      changed_by=d.uploaded_by, note="Original text", created_at=d.created_at))
    db.add(models.DocumentVersion(org_id=d.org_id, document_id=d.id, version=version, title=d.title, content=new,
                                  changed_by=user.id, note=(req.note or "").strip() or "Edited"))
    d.content = new
    d.last_reviewed_at, d.last_reviewed_by = _now(), user.id
    db.query(models.Chunk).filter_by(document_id=d.id).delete()
    for i, c in enumerate(retrieval.chunk_text(new)):
        db.add(models.Chunk(document_id=d.id, org_id=d.org_id, text=c, order_index=i))
    ev = models.ImpactEvent(org_id=d.org_id, kind="document", document_id=d.id, version=version,
                            title=f"{d.title} → v{version}", note=(req.note or "").strip() or None,
                            analysis="{}", created_by=user.id)
    db.add(ev)
    db.flush()
    n = _apply_flags(db, user, ev, analysis, lambda a: _reason_for("document", d, version, a))
    analysis["event_id"] = ev.id
    ev.analysis = json.dumps(analysis)
    _log(db, user.org_id, user.id, "document_edit", f"'{d.title}' v{version}; {n} answer(s) flagged for review")
    db.commit()
    return {"applied": True, "version": version, "event_id": ev.id, "analysis": analysis}


@router.get("/documents/{doc_id}/versions")
def versions(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _doc_for(db, user, doc_id)
    names = _names(db, user.org_id)
    rows = _version_rows(db, d.id)
    if not rows:
        return {"current": 1, "versions": [{"version": 1, "title": d.title, "by": names.get(d.uploaded_by),
                                            "note": "Original text", "at": _iso(d.created_at), "current": True}]}
    cur = rows[-1].version
    return {"current": cur, "versions": [{"version": r.version, "title": r.title, "by": names.get(r.changed_by),
                                          "note": r.note, "at": _iso(r.created_at), "current": r.version == cur}
                                         for r in rows]}


@router.get("/documents/{doc_id}/compare")
def compare(doc_id: int, a: int = Query(...), b: int = Query(...), user: models.User = Depends(auth.get_current_user),
            db: Session = Depends(get_db)):
    d = _doc_for(db, user, doc_id)
    rows = {r.version: r for r in _version_rows(db, d.id)}

    def text_of(v):
        if v in rows:
            return rows[v].content
        if not rows and v == 1:
            return d.content
        raise HTTPException(404, f"Version {v} not found")
    return {"a": a, "b": b, "changes": diff_changes(text_of(a), text_of(b))}


@router.get("/documents/{doc_id}/versions/{version}")
def version_text(doc_id: int, version: int, user: models.User = Depends(auth.get_current_user),
                 db: Session = Depends(get_db)):
    d = _doc_for(db, user, doc_id)
    rows = {r.version: r for r in _version_rows(db, d.id)}
    if version in rows:
        r = rows[version]
        return {"version": version, "title": r.title, "content": r.content, "note": r.note, "at": _iso(r.created_at)}
    if not rows and version == 1:
        return {"version": 1, "title": d.title, "content": d.content, "note": "Original text", "at": _iso(d.created_at)}
    raise HTTPException(404, f"Version {version} not found")


class RollbackIn(BaseModel):
    version: int
    note: Optional[str] = None
    preview: bool = False


@router.post("/documents/{doc_id}/rollback")
def rollback(doc_id: int, req: RollbackIn, user: models.User = Depends(auth.get_current_user),
             db: Session = Depends(get_db)):
    """Rollback never deletes history: it creates a NEW version whose text equals the chosen old version."""
    d = _doc_for(db, user, doc_id)
    if not can_edit(user, d):
        raise HTTPException(403, "Only the owner, the department's managers or admins can change this document")
    rows = {r.version: r for r in _version_rows(db, d.id)}
    if req.version not in rows:
        raise HTTPException(404, f"Version {req.version} not found")
    if req.version == _current_version(db, d.id):
        raise HTTPException(400, "That is already the current version")
    note = (req.note or "").strip() or f"Rollback to v{req.version}"
    res = edit_content(doc_id, ContentIn(content=rows[req.version].content, preview=req.preview, note=note), user, db)
    if res.get("applied"):
        _log(db, user.org_id, user.id, "document_rollback", f"'{d.title}' restored v{req.version} as v{res['version']}")
        db.commit()
    return res


def _cited_answers(db: Session, org_id: int, doc_id: int):
    rows = db.query(models.QARecord).filter(
        models.QARecord.org_id == org_id, models.QARecord.answer_text.isnot(None),
        models.QARecord.status != "superseded",
        models.QARecord.sources.like(f'%"document_id": {doc_id}%')).all()
    return [q for q in rows if any(s.get("document_id") == doc_id for s in _sources(q))]


@router.get("/documents/{doc_id}/impact")
def document_impact(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _doc_for(db, user, doc_id)
    names = _names(db, user.org_id)
    cited = _cited_answers(db, user.org_id, d.id)
    flagged = [q for q in cited if q.impact_status == "needs_review"]
    last = None
    ev = db.query(models.ImpactEvent).filter_by(org_id=user.org_id, kind="document", document_id=d.id).order_by(
        models.ImpactEvent.id.desc()).first()
    if ev and (_now() - ev.created_at).days <= BANNER_DAYS:
        try:
            ch = json.loads(ev.analysis or "{}").get("changes", [])
        except Exception:
            ch = []
        last = {"version": ev.version, "at": _iso(ev.created_at), "by": names.get(ev.created_by), "note": ev.note,
                "changes": ch}
    facts = [{"id": f.id, "text": f"{f.subject} {f.relation} {f.object}"} for f in
             db.query(models.Fact).filter(models.Fact.org_id == user.org_id, models.Fact.document_id == d.id,
                                          models.Fact.status != "superseded")]
    labels = {x for f in db.query(models.Fact).filter(models.Fact.org_id == user.org_id,
                                                      models.Fact.document_id == d.id) for x in (f.subject, f.object)}
    related = []
    for o in db.query(models.Document).filter(models.Document.org_id == user.org_id, models.Document.id != d.id):
        if not _company_visible(db, user, o):
            continue
        via = sorted(l.lower() for l in labels if len(l) >= 4 and l.lower() in o.content.lower())
        if via:
            related.append({"id": o.id, "title": o.title, "via": via[:3]})
    show = auth.rank(user) >= MANAGER
    return {"last_change": last, "answer_count": len(cited), "verified_count": sum(1 for q in cited if _is_verified(q)),
            "flagged_count": len(flagged),
            "answers": [{"qa_id": q.id, "question": q.question, "flagged": q.impact_status == "needs_review"}
                        for q in cited if show and retrieval.dept_ok(user, q.department)],
            "facts": facts, "related": related[:6], "versions": _current_version(db, d.id),
            "can_edit": can_edit(user, d)}


def _queue_item(db, qa, names, ev=None):
    return {"qa_id": qa.id, "question": qa.question, "reason": qa.impact_reason, "event_id": qa.impact_event_id,
            "asker": names.get(qa.user_id, "—"), "status": qa.status, "department": qa.department,
            "answer": _final_text(qa), "at": _iso(ev.created_at if ev else qa.created_at)}


@router.get("/impact/overview")
def overview(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    flagged = db.query(models.QARecord).filter(
        models.QARecord.org_id == user.org_id, models.QARecord.impact_status == "needs_review",
        models.QARecord.status != "superseded").all()
    events = {e.id: e for e in db.query(models.ImpactEvent).filter_by(org_id=user.org_id)}
    mine = [_queue_item(db, q, names, events.get(q.impact_event_id)) for q in flagged if q.user_id == user.id]
    is_mgr = auth.rank(user) >= MANAGER
    queue, evs = [], []
    if is_mgr:
        rows = sorted((q for q in flagged if _can_manage(user, q)), key=lambda q: (not _is_verified(q), q.id))
        queue = [_queue_item(db, q, names, events.get(q.impact_event_id)) for q in rows][:60]
        for e in sorted(events.values(), key=lambda e: e.id, reverse=True)[:15]:
            try:
                counts = json.loads(e.analysis or "{}").get("counts", {})
            except Exception:
                counts = {}
            evs.append({"id": e.id, "kind": e.kind, "title": e.title, "by": names.get(e.created_by),
                        "at": _iso(e.created_at), "counts": counts})
    return {"is_manager": is_mgr, "mine": mine, "queue": queue, "events": evs}


@router.get("/impact/events/{event_id}")
def event_detail(event_id: int, user: models.User = Depends(auth.require_role("manager")),
                 db: Session = Depends(get_db)):
    e = db.get(models.ImpactEvent, event_id)
    if not e or e.org_id != user.org_id:
        raise HTTPException(404, "Change not found")
    a = json.loads(e.analysis or "{}")
    a["event_id"], a["applied"] = e.id, True
    return a


@router.get("/impact/fact/{fact_id}")
def fact_dependencies(fact_id: int, user: models.User = Depends(auth.require_role("manager")),
                      db: Session = Depends(get_db)):
    f = db.get(models.Fact, fact_id)
    if not f or f.org_id != user.org_id:
        raise HTTPException(404, "Fact not found")
    return analyze_fact(db, user, f)


def _steps(db: Session, user, qa, flag_qa, ev) -> list:
    A = json.loads(ev.analysis or "{}") if ev else {}
    entry = next((a for a in A.get("answers", []) if a.get("qa_id") == flag_qa.id), None) or {}
    steps = []
    if A.get("kind") == "document":
        dd = A.get("document") or {}
        doc = db.get(models.Document, dd.get("id"))
        vis = doc is not None and _company_visible(db, user, doc)
        if vis:
            steps.append({"kind": "source", "label": "Source document changed", "title": dd.get("title"),
                          "doc_id": dd.get("id"), "version": A.get("version")})
            if entry.get("old") or entry.get("new"):
                steps.append({"kind": "change", "old": entry.get("old"), "new": entry.get("new"),
                              "delta": entry.get("delta")})
            if entry.get("passage"):
                steps.append({"kind": "passage", "text": entry["passage"]})
        else:
            steps.append({"kind": "source", "label": "A source document changed",
                          "title": "(a document you can no longer open)"})
    elif A.get("kind") == "fact":
        f = A.get("fact") or {}
        steps.append({"kind": "fact", "label": "Fact superseded", "old": f.get("old"),
                      "new": f.get("new") or "(a newer fact)"})
    if flag_qa.id != qa.id:
        steps.append({"kind": "answer", "label": "Verified answer it was served from",
                      "text": _final_text(flag_qa), "question": flag_qa.question, "qa_id": flag_qa.id})
    who = "Your answer" if qa.user_id == user.id else f"{_names(db, user.org_id).get(qa.user_id, 'The')}'s answer"
    steps.append({"kind": "answer", "label": who, "qa_id": qa.id, "text": _final_text(qa), "question": qa.question})
    steps.append({"kind": "status", "label": "Therefore this answer needs revalidation", "reason": flag_qa.impact_reason})
    return steps


@router.get("/impact/qa/{qa_id}/why")
def why(qa_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    qa = db.get(models.QARecord, qa_id)
    if not qa or qa.org_id != user.org_id or not (qa.user_id == user.id or _can_manage(user, qa, allow_own=True)):
        raise HTTPException(404, "Answer not found")
    flag = flag_for(db, qa)
    if not flag:
        return {"affected": False, "message": "Nothing this answer relied on has changed."}
    flag_qa = db.get(models.QARecord, flag["flag_qa_id"])
    ev = db.get(models.ImpactEvent, flag["event_id"]) if flag.get("event_id") else None
    return {"affected": True, "steps": _steps(db, user, qa, flag_qa, ev), "reason": flag["reason"],
            "flag_qa_id": flag_qa.id, "event_id": flag.get("event_id"),
            "asker": _names(db, user.org_id).get(qa.user_id), "can_revalidate": _can_manage(user, flag_qa)}


@router.post("/impact/qa/{qa_id}/revalidate")
def revalidate(qa_id: int, req: RevalidateIn, user: models.User = Depends(auth.require_role("manager")),
               db: Session = Depends(get_db)):
    qa = db.get(models.QARecord, qa_id)
    if not qa or not _can_manage(user, qa) or qa.impact_status != "needs_review":
        raise HTTPException(404, "Nothing to revalidate here (or it isn't yours to decide)")
    if req.action == "still_valid":
        qa.impact_status, qa.impact_reason = None, None
        qa.reviewer_id, qa.reviewed_at = user.id, _now()
        if req.note:
            qa.review_note = req.note
        _notify(db, user.org_id, user.id, qa.user_id,
                f"{user.name} checked “{qa.question}” after a knowledge change: the answer is still valid."
                + (f" Note: {req.note}" if req.note else ""), qa_id=qa.id)
        _log(db, user.org_id, user.id, "impact_still_valid", f"#{qa.id} {qa.question}")
        db.commit()
        return {"status": "still_valid"}
    if req.action == "replace":
        ans = (req.new_answer or "").strip()
        if len(ans) < 3:
            raise HTTPException(400, "Write the new, correct answer")
        new = models.QARecord(org_id=qa.org_id, user_id=qa.user_id, department=qa.department, question=qa.question,
                              answer_text=ans, sources="[]", status="corrected", corrected_answer=ans,
                              reviewer_id=user.id, review_note=(req.note or "Replaced after a knowledge change"),
                              reviewed_at=_now())
        db.add(new)
        db.flush()
        qa.status, qa.superseded_by = "superseded", new.id          # the old answer is kept, never deleted
        _notify(db, user.org_id, user.id, qa.user_id,
                f"{user.name} replaced the answer to “{qa.question}” after a knowledge change. New answer: {ans}",
                qa_id=new.id)
        _log(db, user.org_id, user.id, "impact_replaced", f"#{qa.id} -> #{new.id}: {qa.question}")
        db.commit()
        return {"status": "superseded", "new_qa_id": new.id}
    raise HTTPException(400, "action must be still_valid or replace")
