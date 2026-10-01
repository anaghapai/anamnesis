"""Feature pack 2: everything from the Anamnesis feature list that was still missing.

  * Soft delete + 30-day Recycle Bin
  * Per-document access requests (1 / 7 / 30 days) + temporary grants that auto-expire
  * Just-in-time one-time access links (15-60 min)
  * Dual control for critical actions on Restricted documents
  * Automatic access clean-up when someone changes department / is deactivated
  * View tracking (who opened Confidential / Restricted documents, and when)
  * Knowledge Health Score (pure arithmetic, fully explained)
  * Confidence indicator for answers (rules, not AI)
  * Tags, favorites, comments, "who has access", related documents (via the knowledge graph)
  * My Workspace (private uploads, notes, checklists) + Submit-for-approval publish workflow
  * Help & Navigation chatbot (isolated: cannot touch documents)

Nothing here calls a generative model. Every permission check goes through
retrieval.user_can_see_document, the same gate search uses.
"""
import json
import secrets
import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from . import models, auth, retrieval, extract, help_bot, impact
from .db import get_db

router = APIRouter()

VISIBILITIES = ("public", "internal", "confidential", "restricted")
BIN_DAYS = 30
MANAGER = auth.ROLE_RANK["manager"]
ADMIN = auth.ROLE_RANK["admin"]


# ------------------------------------------------------------------ schema ---

_NEW_COLUMNS = {
    "users": [("pending_approval", "BOOLEAN DEFAULT 0")],
    "documents": [("owner_id", "INTEGER"), ("verified_until", "DATETIME"), ("last_reviewed_at", "DATETIME"),
                  ("last_reviewed_by", "INTEGER"), ("deleted_at", "DATETIME"), ("deleted_by", "INTEGER"),
                  ("tags", "TEXT DEFAULT ''"), ("workspace", "TEXT DEFAULT 'company'"),
                  ("submit_department", "TEXT"), ("submit_visibility", "TEXT"), ("submit_note", "TEXT"),
                  ("review_note", "TEXT"), ("submitted_at", "DATETIME")],
    "conflicts": [("proposed_keep_id", "INTEGER"), ("proposed_by", "INTEGER"), ("proposed_note", "TEXT")],
    "qa_records": [("thread_of", "INTEGER")],
}


def ensure_schema(engine):
    """Adds any missing column to an existing anamnesis.db - so upgrading needs no manual
    migration and no data loss. Safe to run on every start."""
    with engine.begin() as con:
        for table, cols in _NEW_COLUMNS.items():
            existing = {r[1] for r in con.execute(sql_text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue
            for name, coltype in cols:
                if name not in existing:
                    con.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))
        con.execute(sql_text("UPDATE documents SET owner_id = uploaded_by WHERE owner_id IS NULL"))
        con.execute(sql_text("UPDATE documents SET workspace = 'company' WHERE workspace IS NULL"))


# ----------------------------------------------------------------- helpers ---

def _now():
    return datetime.datetime.utcnow()


def _log(db: Session, org_id, user_id, action, detail=""):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action, detail=detail))


def _notify(db: Session, org_id, from_id, to_id, kind, text, document_id=None):
    if to_id and to_id != from_id:
        db.add(models.WorkUpdate(org_id=org_id, from_user_id=from_id, to_user_id=to_id, kind=kind,
                                 text=text, document_id=document_id))


def _ws(d) -> str:
    return d.workspace or "company"


def _doc(db: Session, user: models.User, doc_id: int, include_deleted=False) -> models.Document:
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id:
        raise HTTPException(404, "Document not found")
    if d.deleted_at is not None and not include_deleted:
        raise HTTPException(404, "Document not found")
    return d


def _visible_doc(db, user, doc_id) -> models.Document:
    d = _doc(db, user, doc_id)
    if not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    return d


def can_manage(user: models.User, d: models.Document) -> bool:
    """Owner / uploader, the department's managers, admins and the org owner."""
    if user.id in (d.owner_id, d.uploaded_by):
        return True
    r = auth.rank(user)
    if r >= ADMIN:
        return True
    return r >= MANAGER and _ws(d) == "company" and d.department == user.department


def _approvers(db: Session, org_id: int, department: Optional[str], exclude_id=None) -> List[models.User]:
    q = db.query(models.User).filter(models.User.org_id == org_id, models.User.active == True)  # noqa: E712
    if exclude_id:
        q = q.filter(models.User.id != exclude_id)
    users = q.all()
    if department and department != "All":
        mgrs = [u for u in users if u.role == "manager" and u.department == department]
        if mgrs:
            return mgrs
    return [u for u in users if u.role in ("admin", "owner")]


def _names(db, org_id):
    return {u.id: u.name for u in db.query(models.User).filter_by(org_id=org_id)}


def _tags(d) -> List[str]:
    return [t for t in (d.tags or "").split(",") if t.strip()]


def _iso(x):
    return x.isoformat() if x else None


def _allowed_ids(d) -> List[int]:
    return [int(x) for x in (d.allowed_user_ids or "").split(",") if x.strip().isdigit()]


def _set_chunks(db, d, content):
    db.query(models.Chunk).filter_by(document_id=d.id).delete()
    for i, c in enumerate(retrieval.chunk_text(content)):
        db.add(models.Chunk(document_id=d.id, org_id=d.org_id, text=c, order_index=i))


# --------------------------------------------------------------- recycle bin ---

def _hard_delete(db: Session, d: models.Document):
    did = d.id
    for M in (models.FolderItem, models.Favorite, models.DocComment, models.DocView,
              models.DocumentGrant, models.DocAccessRequest, models.JitToken, models.DualControl):
        db.query(M).filter(M.document_id == did).delete()
    db.query(models.Fact).filter(models.Fact.document_id == did).update({"document_id": None})
    db.delete(d)          # chunks cascade


def purge_expired(db: Session) -> int:
    cutoff = _now() - datetime.timedelta(days=BIN_DAYS)
    old = db.query(models.Document).filter(models.Document.deleted_at != None,  # noqa: E711
                                           models.Document.deleted_at < cutoff).all()
    for d in old:
        _log(db, d.org_id, None, "document_purged", f"'{d.title}' removed permanently after {BIN_DAYS} days")
        _hard_delete(db, d)
    if old:
        db.commit()
    return len(old)


@router.delete("/documents/{doc_id}")
def soft_delete(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _doc(db, user, doc_id)
    if not can_manage(user, d):
        raise HTTPException(403, "Only the owner, the department manager or an admin can delete this")
    d.deleted_at, d.deleted_by = _now(), user.id
    _log(db, user.org_id, user.id, "document_deleted", f"'{d.title}' -> recycle bin")
    if d.owner_id and d.owner_id != user.id:
        _notify(db, user.org_id, user.id, d.owner_id, "progress", f"'{d.title}' was moved to the recycle bin by {user.name}")
    db.commit()
    return {"ok": True, "days_to_restore": BIN_DAYS}


@router.get("/recycle-bin")
def recycle_bin(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    purge_expired(db)
    names = _names(db, user.org_id)
    out = []
    for d in db.query(models.Document).filter(models.Document.org_id == user.org_id,
                                              models.Document.deleted_at != None).all():  # noqa: E711
        if not can_manage(user, d):
            continue
        left = BIN_DAYS - (_now() - d.deleted_at).days
        out.append({"id": d.id, "title": d.title, "department": d.department, "visibility": d.visibility,
                    "workspace": _ws(d), "deleted_at": _iso(d.deleted_at), "deleted_by": names.get(d.deleted_by),
                    "days_left": max(0, left)})
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


@router.post("/recycle-bin/{doc_id}/restore")
def restore(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _doc(db, user, doc_id, include_deleted=True)
    if d.deleted_at is None or not can_manage(user, d):
        raise HTTPException(404, "Nothing to restore")
    d.deleted_at = d.deleted_by = None
    _log(db, user.org_id, user.id, "document_restored", d.title)
    db.commit()
    return {"ok": True}


@router.delete("/recycle-bin/{doc_id}")
def delete_forever(doc_id: int, user: models.User = Depends(auth.require_role("admin")), db: Session = Depends(get_db)):
    d = _doc(db, user, doc_id, include_deleted=True)
    if d.deleted_at is None:
        raise HTTPException(400, "Move it to the recycle bin first")
    _log(db, user.org_id, user.id, "document_purged_by_admin", d.title)
    _hard_delete(db, d)
    db.commit()
    return {"ok": True}


# -------------------------------------------------- per-document access requests ---

class DocAccessIn(BaseModel):
    document_id: int
    reason: str = ""
    days: int = 1


class DocAccessDecide(BaseModel):
    approve: bool
    days: Optional[int] = None


@router.post("/doc-access-requests")
def request_doc_access(req: DocAccessIn, user: models.User = Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    """Deliberately answers the same way whether or not the document exists - it never
    confirms that a document you can't open is real."""
    if req.days not in (1, 7, 30):
        raise HTTPException(400, "Choose 1, 7 or 30 days")
    generic = {"ok": True, "message": "Request sent. If the document exists, its owner or manager will decide."}
    d = db.query(models.Document).get(req.document_id)
    if not d or d.org_id != user.org_id or d.deleted_at is not None or _ws(d) != "company":
        return generic
    if retrieval.user_can_see_document(user, d, db):
        return {"ok": True, "message": "You can already open this document."}
    dup = db.query(models.DocAccessRequest).filter_by(document_id=d.id, requester_id=user.id, status="pending").first()
    if dup:
        return generic
    ar = models.DocAccessRequest(org_id=user.org_id, document_id=d.id, requester_id=user.id,
                                 reason=req.reason.strip(), days=req.days)
    db.add(ar)
    targets = {d.owner_id} | {u.id for u in _approvers(db, user.org_id, d.department)}
    for t in targets:
        _notify(db, user.org_id, user.id, t, "access_request",
                f"{user.name} requests {req.days}-day access to '{d.title}'"
                + (f" - {req.reason.strip()}" if req.reason.strip() else ""), document_id=d.id)
    _log(db, user.org_id, user.id, "doc_access_requested", f"doc #{d.id}, {req.days}d")
    db.commit()
    return generic


def _can_decide_doc_access(user, d) -> bool:
    return d is not None and (can_manage(user, d) or auth.rank(user) >= ADMIN)


@router.get("/doc-access-requests")
def list_doc_access(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    mine = db.query(models.DocAccessRequest).filter_by(org_id=user.org_id, requester_id=user.id) \
        .order_by(models.DocAccessRequest.created_at.desc()).limit(30).all()
    review = []
    for a in db.query(models.DocAccessRequest).filter_by(org_id=user.org_id, status="pending").all():
        d = db.query(models.Document).get(a.document_id)
        if d and d.deleted_at is None and _can_decide_doc_access(user, d) and a.requester_id != user.id:
            review.append(a)

    def out(a):
        d = db.query(models.Document).get(a.document_id)
        mine_row = a.requester_id == user.id
        return {"id": a.id, "document_id": a.document_id,
                # a requester only learns the title after it is approved
                "title": d.title if d and (not mine_row or a.status == "approved") else f"Document #{a.document_id}",
                "requester": names.get(a.requester_id), "reason": a.reason, "days": a.days,
                "status": a.status, "created_at": _iso(a.created_at)}
    return {"mine": [out(a) for a in mine], "for_review": [out(a) for a in review]}


@router.post("/doc-access-requests/{req_id}/decide")
def decide_doc_access(req_id: int, req: DocAccessDecide, user: models.User = Depends(auth.get_current_user),
                      db: Session = Depends(get_db)):
    a = db.query(models.DocAccessRequest).get(req_id)
    d = db.query(models.Document).get(a.document_id) if a else None
    if not a or a.org_id != user.org_id or not _can_decide_doc_access(user, d):
        raise HTTPException(404, "No such request")
    if a.requester_id == user.id:
        raise HTTPException(403, "You can't approve your own request")
    if a.status != "pending":
        raise HTTPException(400, "Already decided")
    days = req.days if req.days in (1, 7, 30) else a.days
    a.status = "approved" if req.approve else "denied"
    a.decided_by, a.decided_at = user.id, _now()
    if req.approve:
        db.add(models.DocumentGrant(org_id=user.org_id, document_id=d.id, user_id=a.requester_id, granted_by=user.id,
                                    source="request", expires_at=_now() + datetime.timedelta(days=days)))
    _notify(db, user.org_id, user.id, a.requester_id, "access_decision",
            f"Your request for '{d.title}' was {a.status}" + (f" for {days} day(s)" if req.approve else ""),
            document_id=d.id if req.approve else None)
    _log(db, user.org_id, user.id, "doc_access_" + a.status, f"doc #{d.id} for user #{a.requester_id}")
    db.commit()
    return {"ok": True, "status": a.status}


# ------------------------------------- cross-department request by description ---

class DocFindIn(BaseModel):
    department: str
    description: str = Field(min_length=3, max_length=300)
    reason: str = Field(default="", max_length=500)
    days: int = 1


class DocFindDecide(BaseModel):
    approve: bool
    document_ids: List[int] = []
    days: Optional[int] = None


def _can_decide_find(user, department: str) -> bool:
    r = auth.rank(user)
    return r >= ADMIN or (r >= MANAGER and user.department == department)


def _words(text: str) -> set:
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def _find_candidates(db, approver, ar):
    """Documents of the request's department that the approver manages and the requester
    can't already open, best description match first. Only the approver ever sees this list."""
    want = _words(ar.description)
    requester = db.query(models.User).get(ar.requester_id)
    rows = []
    for d in db.query(models.Document).filter_by(org_id=ar.org_id, department=ar.department).all():
        if d.deleted_at is not None or _ws(d) != "company" or not can_manage(approver, d):
            continue
        if requester and retrieval.user_can_see_document(requester, d, db):
            continue
        have = _words(d.title) | _words(d.tags) | _words((d.content or "")[:1500])
        score = len(want & have) + 2 * len(want & _words(d.title))
        rows.append({"id": d.id, "title": d.title, "visibility": d.visibility, "match": score})
    rows.sort(key=lambda r: (-r["match"], r["title"].lower()))
    return rows[:60]


@router.post("/doc-requests")
def request_doc_by_description(req: DocFindIn, user: models.User = Depends(auth.get_current_user),
                               db: Session = Depends(get_db)):
    if req.days not in (1, 7, 30):
        raise HTTPException(400, "Choose 1, 7 or 30 days")
    org = db.query(models.Organization).get(user.org_id)
    depts = [x.strip() for x in (org.departments or "").split(",") if x.strip()]
    if req.department not in depts:
        raise HTTPException(400, "Choose a department")
    if req.department == user.department:
        raise HTTPException(400, "That's your own department. You can already open its documents.")
    desc = req.description.strip()
    if len(desc) < 3:
        raise HTTPException(400, "Describe the document you need")
    dup = db.query(models.DocFindRequest).filter_by(org_id=user.org_id, requester_id=user.id,
                                                    department=req.department, description=desc,
                                                    status="pending").first()
    if not dup:
        db.add(models.DocFindRequest(org_id=user.org_id, requester_id=user.id, department=req.department,
                                     description=desc, reason=req.reason.strip(), days=req.days))
        for a in _approvers(db, user.org_id, req.department, exclude_id=user.id):
            _notify(db, user.org_id, user.id, a.id, "access_request",
                    f"{user.name} needs a {req.department} document ({req.days}-day access): {desc[:120]}")
        _log(db, user.org_id, user.id, "doc_access_find_requested", f"{req.department}, {req.days}d")
        db.commit()
    return {"ok": True, "message": "Request sent. The department's approver will pick the matching document."}


@router.get("/doc-requests")
def list_doc_find_requests(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    mine = db.query(models.DocFindRequest).filter_by(org_id=user.org_id, requester_id=user.id) \
        .order_by(models.DocFindRequest.created_at.desc()).limit(30).all()
    review = [a for a in db.query(models.DocFindRequest).filter_by(org_id=user.org_id, status="pending")
              .order_by(models.DocFindRequest.created_at).all()
              if a.requester_id != user.id and _can_decide_find(user, a.department)]

    def out(a, is_mine):
        item = {"id": a.id, "department": a.department, "description": a.description, "reason": a.reason,
                "days": a.days, "status": a.status, "created_at": _iso(a.created_at),
                "requester": names.get(a.requester_id)}
        if is_mine and a.status == "approved":
            ids = [int(x) for x in (a.granted_ids or "").split(",") if x.strip().isdigit()]
            docs = [db.query(models.Document).get(i) for i in ids]
            item["documents"] = [{"id": d.id, "title": d.title} for d in docs if d and d.deleted_at is None]
        return item
    return {"mine": [out(a, True) for a in mine], "for_review": [out(a, False) for a in review]}


@router.get("/doc-requests/{req_id}/candidates")
def doc_find_candidates(req_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    a = db.query(models.DocFindRequest).get(req_id)
    if not a or a.org_id != user.org_id or a.requester_id == user.id or not _can_decide_find(user, a.department):
        raise HTTPException(404, "No such request")
    return {"documents": _find_candidates(db, user, a)}


@router.post("/doc-requests/{req_id}/decide")
def decide_doc_find(req_id: int, req: DocFindDecide, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    a = db.query(models.DocFindRequest).get(req_id)
    if not a or a.org_id != user.org_id or not _can_decide_find(user, a.department):
        raise HTTPException(404, "No such request")
    if a.requester_id == user.id:
        raise HTTPException(403, "You can't approve your own request")
    if a.status != "pending":
        raise HTTPException(400, "Already decided")
    days = req.days if req.days in (1, 7, 30) else a.days
    picked = []
    if req.approve:
        ids = list(dict.fromkeys(req.document_ids))
        if not ids:
            raise HTTPException(400, "Pick the document to give access to")
        for i in ids:
            d = db.query(models.Document).get(i)
            if (not d or d.org_id != user.org_id or d.deleted_at is not None or _ws(d) != "company"
                    or d.department != a.department or not can_manage(user, d)):
                raise HTTPException(400, "One of the chosen documents isn't available for this request")
            picked.append(d)
    a.status = "approved" if req.approve else "denied"
    a.decided_by, a.decided_at = user.id, _now()
    if picked:
        a.granted_ids = ",".join(str(d.id) for d in picked)
        for d in picked:
            db.add(models.DocumentGrant(org_id=user.org_id, document_id=d.id, user_id=a.requester_id,
                                        granted_by=user.id, source="request",
                                        expires_at=_now() + datetime.timedelta(days=days)))
    _notify(db, user.org_id, user.id, a.requester_id, "access_decision",
            (f"Your request was approved for {days} day(s): " + ", ".join(f"'{d.title}'" for d in picked))
            if picked else f"Your request for a {a.department} document ('{a.description[:80]}') was denied",
            document_id=picked[0].id if picked else None)
    _log(db, user.org_id, user.id, "doc_access_find_" + a.status,
         f"request #{a.id} for user #{a.requester_id}" + (f", docs {a.granted_ids}" if picked else ""))
    db.commit()
    return {"ok": True, "status": a.status}


@router.get("/documents/{doc_id}/access")
def who_has_access(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if not can_manage(user, d) and auth.rank(user) < MANAGER:
        raise HTTPException(403, "Only the owner or a manager can see the access list")
    now = _now()
    grants = {}
    for g in db.query(models.DocumentGrant).filter(models.DocumentGrant.document_id == d.id,
                                                   models.DocumentGrant.expires_at > now):
        grants.setdefault(g.user_id, []).append(g)
    out = []
    for u in db.query(models.User).filter_by(org_id=user.org_id, active=True):
        if not retrieval.user_can_see_document(u, d, db):
            continue
        if u.id in grants:
            g = max(grants[u.id], key=lambda x: x.expires_at)
            why = f"temporary ({g.source}) until {g.expires_at.strftime('%d %b %H:%M')} UTC"
        elif u.id == d.owner_id:
            why = "document owner"
        elif d.visibility == "restricted":
            why = "org owner" if u.role == "owner" else "named on the document"
        elif u.role in ("admin", "owner"):
            why = f"{u.role} (sees everything)"
        elif u.department == d.department:
            why = "same department"
        elif d.department == "All":
            why = "company-wide document"
        else:
            why = "temporary department grant"
        out.append({"id": u.id, "name": u.name, "role": u.role, "department": u.department, "why": why})
    return {"visibility": d.visibility, "department": d.department, "people": out}


# ------------------------------------------------------------------ JIT links ---

class JitIn(BaseModel):
    minutes: int = Field(default=30, ge=15, le=60)


class JitRedeem(BaseModel):
    token: str


@router.post("/documents/{doc_id}/jit")
def create_jit(doc_id: int, req: JitIn, user: models.User = Depends(auth.get_current_user),
               db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if d.visibility not in ("confidential", "restricted"):
        raise HTTPException(400, "One-time links are for Confidential or Restricted documents")
    if not can_manage(user, d):
        raise HTTPException(403, "Only the document's owner or manager can create a one-time link")
    t = models.JitToken(org_id=user.org_id, document_id=d.id, token=secrets.token_urlsafe(24), created_by=user.id,
                        expires_at=_now() + datetime.timedelta(minutes=req.minutes))
    db.add(t)
    _log(db, user.org_id, user.id, "jit_created", f"'{d.title}' valid {req.minutes} min")
    db.commit()
    return {"token": t.token, "path": f"/?jit={t.token}", "expires_at": _iso(t.expires_at), "minutes": req.minutes}


@router.post("/jit/redeem")
def redeem_jit(req: JitRedeem, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.JitToken).filter_by(token=req.token.strip()).first()
    bad = HTTPException(400, "This link is invalid, expired or has already been used")
    if not t or t.org_id != user.org_id or t.used_at is not None or t.expires_at < _now():
        raise bad
    d = db.query(models.Document).get(t.document_id)
    if not d or d.deleted_at is not None:
        raise bad
    t.used_by, t.used_at = user.id, _now()
    db.add(models.DocumentGrant(org_id=user.org_id, document_id=d.id, user_id=user.id, granted_by=t.created_by,
                                source="jit", expires_at=t.expires_at))
    _log(db, user.org_id, user.id, "jit_redeemed", f"'{d.title}' via one-time link")
    _notify(db, user.org_id, user.id, t.created_by, "progress", f"{user.name} opened '{d.title}' with your one-time link",
            document_id=d.id)
    db.commit()
    return {"document_id": d.id, "title": d.title, "expires_at": _iso(t.expires_at)}


# ---------------------------------------------------------------- dual control ---

class ClassifyIn(BaseModel):
    visibility: str
    allowed_user_ids: Optional[List[int]] = None


class PermanentIn(BaseModel):
    user_id: int


class DualDecide(BaseModel):
    approve: bool
    note: Optional[str] = None


def _apply_dual(db, dc: models.DualControl, d: models.Document, actor=None):
    p = json.loads(dc.payload or "{}")
    if dc.action == "classify":
        d.visibility = p["visibility"]
        if p.get("allowed_user_ids") is not None:
            d.allowed_user_ids = ",".join(str(i) for i in p["allowed_user_ids"])
    elif dc.action == "permanent_access":
        ids = set(_allowed_ids(d)) | {int(p["user_id"])}
        d.allowed_user_ids = ",".join(str(i) for i in sorted(ids))
    elif dc.action == "content_edit":
        old, new = p["old_text"], p["new_text"]
        if (d.content or "").count(old) != 1:
            raise HTTPException(409, "The document changed since this was proposed, so the wording can't be applied. "
                                     "Reject it and ask for a new correction.")
        impact.apply_content_edit(db, actor, d, d.content.replace(old, new, 1),
                                  f"Corrected after review of: {p.get('question', '')[:120]}")


@router.post("/documents/{doc_id}/classify")
def change_classification(doc_id: int, req: ClassifyIn, user: models.User = Depends(auth.get_current_user),
                          db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if req.visibility not in VISIBILITIES:
        raise HTTPException(400, "Invalid classification")
    if not can_manage(user, d) or auth.rank(user) < MANAGER:
        raise HTTPException(403, "Only a manager who manages this document can change its classification")
    if req.visibility == d.visibility and req.allowed_user_ids is None:
        return {"ok": True, "applied": True}
    payload = {"visibility": req.visibility, "allowed_user_ids": req.allowed_user_ids}
    if d.visibility == "restricted":                       # loosening a Restricted document -> two people
        dc = models.DualControl(org_id=user.org_id, document_id=d.id, action="classify",
                                payload=json.dumps(payload), requested_by=user.id)
        db.add(dc)
        for a in _approvers(db, user.org_id, None, exclude_id=user.id):
            _notify(db, user.org_id, user.id, a.id, "review_request",
                    f"Second approval needed: {user.name} wants '{d.title}' changed from Restricted to {req.visibility}",
                    document_id=d.id)
        _log(db, user.org_id, user.id, "dual_control_requested", f"classify '{d.title}' -> {req.visibility}")
        db.commit()
        return {"ok": True, "applied": False, "message": "Needs a second approver - it's in the Approvals queue."}
    d.visibility = req.visibility
    if req.visibility == "restricted":                     # tightening is safe: author + requester keep access
        ids = set(req.allowed_user_ids or _allowed_ids(d)) | {user.id} | ({d.owner_id} if d.owner_id else set())
        d.allowed_user_ids = ",".join(str(i) for i in sorted(ids))
    _log(db, user.org_id, user.id, "classification_changed", f"'{d.title}' -> {req.visibility}")
    db.commit()
    return {"ok": True, "applied": True}


@router.post("/documents/{doc_id}/permanent-access")
def request_permanent_access(doc_id: int, req: PermanentIn, user: models.User = Depends(auth.get_current_user),
                             db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if d.visibility != "restricted":
        raise HTTPException(400, "Named-person access lists only apply to Restricted documents. "
                                 "For other documents use a temporary grant.")
    if not can_manage(user, d) or auth.rank(user) < MANAGER:
        raise HTTPException(403, "Only a manager who manages this document can request permanent access")
    target = db.query(models.User).get(req.user_id)
    if not target or target.org_id != user.org_id or not target.active:
        raise HTTPException(404, "No such person")
    dc = models.DualControl(org_id=user.org_id, document_id=d.id, action="permanent_access",
                            payload=json.dumps({"user_id": target.id, "name": target.name}), requested_by=user.id)
    db.add(dc)
    for a in _approvers(db, user.org_id, None, exclude_id=user.id):
        _notify(db, user.org_id, user.id, a.id, "review_request",
                f"Second approval needed: permanent access to '{d.title}' for {target.name}", document_id=d.id)
    _log(db, user.org_id, user.id, "dual_control_requested", f"permanent access '{d.title}' for {target.name}")
    db.commit()
    return {"ok": True, "message": "Needs a second approver - it's in the Approvals queue."}


@router.get("/dual-control")
def list_dual(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    out = []
    for dc in db.query(models.DualControl).filter_by(org_id=user.org_id).order_by(models.DualControl.created_at.desc()).limit(50):
        d = db.query(models.Document).get(dc.document_id)
        p = json.loads(dc.payload or "{}")
        what = (f"Change classification to {p.get('visibility')}" if dc.action == "classify"
                else "Correct the wording in this document" if dc.action == "content_edit"
                else f"Give {p.get('name', 'user #' + str(p.get('user_id')))} permanent access")
        detail = {"old": p.get("old_text"), "new": p.get("new_text")} if dc.action == "content_edit" else None
        can_dec = dc.status == "pending" and dc.requested_by != user.id
        if dc.action == "content_edit" and d is not None:
            can_dec = can_dec and impact._is_doc_owner(user, d)
        out.append({"id": dc.id, "document_id": dc.document_id, "title": d.title if d else "(deleted)",
                    "action": what, "requested_by": names.get(dc.requested_by), "status": dc.status,
                    "can_decide": can_dec, "detail": detail,
                    "created_at": _iso(dc.created_at), "decided_by": names.get(dc.decided_by), "note": dc.note})
    return out


@router.post("/dual-control/{dc_id}/decide")
def decide_dual(dc_id: int, req: DualDecide, user: models.User = Depends(auth.require_role("manager")),
                db: Session = Depends(get_db)):
    dc = db.query(models.DualControl).get(dc_id)
    if not dc or dc.org_id != user.org_id:
        raise HTTPException(404, "No such request")
    if dc.status != "pending":
        raise HTTPException(400, "Already decided")
    if dc.requested_by == user.id:
        raise HTTPException(403, "Dual control: a different person must approve this")
    d = db.query(models.Document).get(dc.document_id)
    if not d:
        raise HTTPException(404, "Document no longer exists")
    if dc.action == "content_edit" and not impact._is_doc_owner(user, d):
        raise HTTPException(403, "Only the document's owner or an admin can approve a change to its text")
    dc.status = "approved" if req.approve else "rejected"
    dc.decided_by, dc.decided_at, dc.note = user.id, _now(), req.note
    if req.approve:
        _apply_dual(db, dc, d, user)
    _notify(db, user.org_id, user.id, dc.requested_by, "review_result",
            f"Your request on '{d.title}' was {dc.status} by {user.name}", document_id=d.id)
    _log(db, user.org_id, user.id, f"dual_control_{dc.status}", f"{dc.action} on '{d.title}'")
    db.commit()
    return {"ok": True, "status": dc.status}


# --------------------------------------------------------- access clean-up ---

def revoke_extra_access(db: Session, emp: models.User, actor_id=None, reason="") -> dict:
    """Called when someone changes department or is deactivated: every temporary grant,
    pending access request and named-person entry (except for their own documents) goes."""
    n_dept = db.query(models.DepartmentGrant).filter_by(user_id=emp.id).delete()
    n_doc = db.query(models.DocumentGrant).filter_by(user_id=emp.id).delete()
    db.query(models.DocAccessRequest).filter_by(requester_id=emp.id, status="pending").update({"status": "denied"})
    db.query(models.DocFindRequest).filter_by(requester_id=emp.id, status="pending").update({"status": "denied"})
    db.query(models.AccessRequest).filter_by(requester_id=emp.id, status="pending").update({"status": "denied"})
    n_named = 0
    for d in db.query(models.Document).filter_by(org_id=emp.org_id, visibility="restricted"):
        ids = _allowed_ids(d)
        if emp.id in ids and emp.id not in (d.owner_id, d.uploaded_by):
            d.allowed_user_ids = ",".join(str(i) for i in ids if i != emp.id)
            n_named += 1
    _log(db, emp.org_id, actor_id, "access_cleanup",
         f"{emp.name} ({reason}): {n_dept} dept grants, {n_doc} document grants, {n_named} named-access entries removed")
    return {"dept_grants": n_dept, "doc_grants": n_doc, "named": n_named}


# ------------------------------------------------------------- view tracking ---

def record_view(db: Session, user: models.User, d: models.Document):
    if d.visibility in ("confidential", "restricted"):
        db.add(models.DocView(org_id=d.org_id, document_id=d.id, user_id=user.id))


@router.get("/documents/{doc_id}/views")
def doc_views(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if not can_manage(user, d) and auth.rank(user) < ADMIN:
        raise HTTPException(403, "Only the owner or an admin can see the view history")
    names = _names(db, user.org_id)
    rows = db.query(models.DocView).filter_by(document_id=d.id).order_by(models.DocView.created_at.desc()).limit(100)
    return [{"user": names.get(v.user_id), "at": _iso(v.created_at)} for v in rows]


# ---------------------------------------------------------- knowledge health ---

@router.get("/health-score")
def health_score(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """0-100, pure arithmetic. Every point is explained so nobody has to trust a black box."""
    now = _now()
    docs = [d for d in db.query(models.Document).filter_by(org_id=user.org_id)
            if d.deleted_at is None and _ws(d) == "company" and retrieval.user_can_see_document(user, d, db)]
    total = len(docs)
    recent = [d for d in docs if d.last_reviewed_at and (now - d.last_reviewed_at).days <= 90]
    pct = (len(recent) / total) if total else 1.0
    o = user.org_id
    conflicts = db.query(models.Conflict).filter_by(org_id=o, resolved=False).count()
    flags = db.query(models.QARecord).filter_by(org_id=o, status="flagged").count()
    ver = db.query(models.QARecord).filter(models.QARecord.org_id == o,
                                           models.QARecord.status.in_(["verified", "corrected"])).all()
    ages = [(now - (v.reviewed_at or v.created_at)).days for v in ver]
    avg_age = (sum(ages) / len(ages)) if ages else None
    missing = [d for d in docs if not d.owner_id or d.visibility not in VISIBILITIES]

    parts = [
        {"key": "reviewed", "label": "Documents reviewed in the last 90 days", "max": 30,
         "points": round(30 * pct, 1),
         "detail": f"{len(recent)} of {total} documents ({round(pct * 100)}%) - 30 x {round(pct, 2)}"},
        {"key": "conflicts", "label": "Unresolved conflicts", "max": 20,
         "points": max(0, 20 - 4 * conflicts), "detail": f"{conflicts} open - each one costs 4 points"},
        {"key": "flags", "label": "Open flags on answers", "max": 20,
         "points": max(0, 20 - 4 * flags), "detail": f"{flags} waiting for review - each one costs 4 points"},
        {"key": "age", "label": "Average age of verified answers", "max": 15,
         "points": round(15 * max(0.0, 1 - (avg_age or 0) / 180), 1),
         "detail": (f"{round(avg_age)} days on average - loses points linearly, zero at 180 days"
                    if avg_age is not None else "No verified answers yet - full marks")},
        {"key": "metadata", "label": "Documents with an owner and a classification", "max": 15,
         "points": round(15 * (1 - (len(missing) / total if total else 0)), 1),
         "detail": f"{len(missing)} of {total} are missing an owner or classification"},
    ]
    score = round(sum(p["points"] for p in parts))
    stale = sorted([d for d in docs if d not in recent], key=lambda d: d.last_reviewed_at or d.created_at)[:6]
    return {"score": score, "grade": "Healthy" if score >= 80 else "Needs attention" if score >= 55 else "At risk",
            "parts": parts, "formula": "reviewed (30) + conflicts (20) + flags (20) + verified-answer age (15) + owner/classification (15)",
            "needs_review": [{"id": d.id, "title": d.title, "last_reviewed_at": _iso(d.last_reviewed_at)} for d in stale]}


# ---------------------------------------------------------------- confidence ---

def compute_confidence(db: Session, answer: Optional[dict], documents: list, verified: Optional[dict]) -> Optional[dict]:
    """High / Medium / Low from three plain signals: match strength, freshness, verification."""
    if not answer and not verified:
        return None
    reasons, pts = [], 0
    if verified:
        pts += 3
        reasons.append("A person verified this answer" + (f" ({verified.get('reviewer')})" if verified.get("reviewer") else ""))
    if answer:
        grp = next((g for g in documents if g.get("document_id") == answer.get("document_id")), None)
        score = (grp or {}).get("score", 0)
        if score >= 0.6:
            pts += 2; reasons.append("Strong match with your question")
        elif score >= 0.4:
            pts += 1; reasons.append("Fair match with your question")
        else:
            reasons.append("Weak match - the wording differs from your question")
        d = db.query(models.Document).get(answer["document_id"])
        now = _now()
        if d and d.verified_until and d.verified_until >= now:
            pts += 2; reasons.append(f"Source document verified until {d.verified_until.strftime('%d %b %Y')}")
        elif d:
            age = (now - max(d.created_at, d.last_reviewed_at or d.created_at)).days
            if age <= 90:
                pts += 1; reasons.append(f"Source document is recent ({age} days since last review)")
            else:
                reasons.append(f"Source document is {age} days old and not verified - may be outdated")
    level = "High" if pts >= 4 else "Medium" if pts >= 2 else "Low"
    return {"level": level, "points": pts, "reasons": reasons}


# ---------------------------------------------- tags / favorites / comments / related ---

class TagsIn(BaseModel):
    tags: List[str]


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.put("/documents/{doc_id}/tags")
def set_tags(doc_id: int, req: TagsIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if not can_manage(user, d):
        raise HTTPException(403, "Only the owner or a manager can edit tags")
    clean = []
    for t in req.tags:
        t = "".join(ch for ch in t.strip().lower() if ch.isalnum() or ch in "-_ ").strip().replace(" ", "-")[:24]
        if t and t not in clean:
            clean.append(t)
    d.tags = ",".join(clean[:12])
    db.commit()
    return {"tags": _tags(d)}


@router.post("/documents/{doc_id}/favorite")
def toggle_favorite(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    f = db.query(models.Favorite).filter_by(user_id=user.id, document_id=d.id).first()
    if f:
        db.delete(f)
    else:
        db.add(models.Favorite(user_id=user.id, document_id=d.id))
    db.commit()
    return {"pinned": not f}


@router.get("/documents-meta")
def documents_meta(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    fav = {f.document_id for f in db.query(models.Favorite).filter_by(user_id=user.id)}
    counts = {}
    for c in db.query(models.DocComment).filter_by(org_id=user.org_id):
        counts[c.document_id] = counts.get(c.document_id, 0) + 1
    out = {}
    for d in db.query(models.Document).filter_by(org_id=user.org_id):
        if d.deleted_at is None and _ws(d) == "company" and retrieval.user_can_see_document(user, d, db):
            out[d.id] = {"tags": _tags(d), "pinned": d.id in fav, "comments": counts.get(d.id, 0)}
    return out


@router.post("/documents/{doc_id}/comments")
def add_comment(doc_id: int, req: CommentIn, user: models.User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    if _ws(d) != "company":
        raise HTTPException(400, "Comments are for official company documents")
    db.add(models.DocComment(org_id=user.org_id, document_id=d.id, user_id=user.id, text=req.text.strip()))
    if d.owner_id:
        _notify(db, user.org_id, user.id, d.owner_id, "progress", f"{user.name} commented on '{d.title}'", document_id=d.id)
    db.commit()
    return {"ok": True}


@router.delete("/comments/{cid}")
def delete_comment(cid: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = db.query(models.DocComment).get(cid)
    if not c or c.org_id != user.org_id:
        raise HTTPException(404, "No such comment")
    if c.user_id != user.id and auth.rank(user) < MANAGER:
        raise HTTPException(403, "You can only delete your own comments")
    db.delete(c)
    db.commit()
    return {"ok": True}


def _related(db, user, d, limit=5):
    """Related through the knowledge graph: other documents whose facts mention the same entities."""
    mine = db.query(models.Fact).filter(models.Fact.document_id == d.id, models.Fact.status != "superseded").all()
    ents = {x.strip().lower() for f in mine for x in (f.subject, f.object) if x and x.strip()}
    found = {}
    if ents:
        for f in db.query(models.Fact).filter(models.Fact.org_id == d.org_id, models.Fact.document_id != None,  # noqa: E711
                                              models.Fact.document_id != d.id, models.Fact.status != "superseded"):
            shared = {x.strip().lower() for x in (f.subject, f.object)} & ents
            if shared:
                found.setdefault(f.document_id, set()).update(shared)
    out = []
    for did, shared in sorted(found.items(), key=lambda kv: -len(kv[1])):
        o = db.query(models.Document).get(did)
        if o and o.deleted_at is None and _ws(o) == "company" and retrieval.user_can_see_document(user, o, db):
            out.append({"id": o.id, "title": o.title, "via": sorted(shared)[:3], "kind": "graph"})
    if len(out) < limit and _tags(d):                      # fallback: same tag
        mytags = set(_tags(d))
        for o in db.query(models.Document).filter(models.Document.org_id == d.org_id, models.Document.id != d.id):
            if o.deleted_at is None and _ws(o) == "company" and mytags & set(_tags(o)) \
                    and all(x["id"] != o.id for x in out) and retrieval.user_can_see_document(user, o, db):
                out.append({"id": o.id, "title": o.title, "via": sorted(mytags & set(_tags(o))), "kind": "tag"})
    return out[:limit]


@router.get("/documents/{doc_id}/extras")
def doc_extras(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _visible_doc(db, user, doc_id)
    names = _names(db, user.org_id)
    comments = db.query(models.DocComment).filter_by(document_id=d.id).order_by(models.DocComment.created_at).all()
    pinned = db.query(models.Favorite).filter_by(user_id=user.id, document_id=d.id).first() is not None
    manage = can_manage(user, d)
    return {"tags": _tags(d), "pinned": pinned, "workspace": _ws(d), "can_manage": manage,
            "review_note": d.review_note if d.uploaded_by == user.id else None,
            "comments": [{"id": c.id, "user": names.get(c.user_id), "text": c.text, "at": _iso(c.created_at),
                          "can_delete": c.user_id == user.id or auth.rank(user) >= MANAGER} for c in comments],
            "related": _related(db, user, d),
            "can_jit": manage and d.visibility in ("confidential", "restricted"),
            "can_classify": manage and auth.rank(user) >= MANAGER,
            "can_see_access": manage or auth.rank(user) >= MANAGER,
            "can_delete": manage}


# ---------------------------------------------------- My Workspace + approvals ---

class WsDocIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=10)


class WsDocEdit(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class SubmitIn(BaseModel):
    department: str
    visibility: str = "internal"
    note: str = ""


class ApprovalDecide(BaseModel):
    decision: str                       # approve | reject | changes
    note: str = ""
    department: Optional[str] = None
    visibility: Optional[str] = None


class NoteIn(BaseModel):
    kind: str = "note"
    title: str = Field(min_length=1, max_length=120)
    body: str = ""


def _mine(db, user, doc_id) -> models.Document:
    d = _doc(db, user, doc_id)
    if d.uploaded_by != user.id or _ws(d) == "company":
        raise HTTPException(404, "Document not found")
    return d


def _new_private_doc(db, user, title, content, filename=None, ftype="text"):
    if auth.rank(user) < auth.ROLE_RANK["intern"]:
        raise HTTPException(403, "Guests don't have a workspace")
    d = models.Document(org_id=user.org_id, uploaded_by=user.id, owner_id=user.id, title=title, content=content,
                        visibility="internal", department=user.department, filename=filename, file_type=ftype,
                        allowed_user_ids="", workspace="personal")
    db.add(d)
    db.flush()
    _set_chunks(db, d, content)
    _log(db, user.org_id, user.id, "workspace_upload", f"'{title}' (private)")
    db.commit()
    return {"id": d.id, "title": d.title}


def _ws_doc_out(d):
    status = "pending" if _ws(d) == "pending" else "private"
    note = d.review_note or ""
    if status == "private" and note:
        status = "rejected" if note.startswith("rejected") else "changes"
    return {"id": d.id, "title": d.title, "file_type": d.file_type, "status": status, "review_note": note or None,
            "submit_department": d.submit_department, "submit_visibility": d.submit_visibility,
            "submitted_at": _iso(d.submitted_at), "created_at": _iso(d.created_at), "length": len(d.content or ""),
            "preview": " ".join((d.content or "").split())[:200]}


def _note_out(n):
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "updated_at": _iso(n.updated_at)}


@router.get("/workspace")
def my_workspace(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    docs = db.query(models.Document).filter(models.Document.uploaded_by == user.id, models.Document.deleted_at == None,  # noqa: E711
                                            models.Document.workspace != "company").order_by(models.Document.created_at.desc()).all()
    notes = db.query(models.Note).filter_by(user_id=user.id).order_by(models.Note.updated_at.desc()).all()
    return {"documents": [_ws_doc_out(d) for d in docs], "notes": [_note_out(n) for n in notes]}


@router.post("/workspace/documents")
def ws_paste(req: WsDocIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return _new_private_doc(db, user, req.title.strip(), req.content)


@router.post("/workspace/upload")
async def ws_upload(file: UploadFile = File(...), title: str = Form(""),
                    user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (10 MB max)")
    try:
        text = extract.extract_text(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Could not read this file - is it corrupted or password-protected?")
    if len(text.strip()) < 10:
        raise HTTPException(400, "No extractable text found (scanned images need OCR, which isn't included).")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    return _new_private_doc(db, user, title.strip() or file.filename, text, file.filename, ext)


@router.patch("/workspace/documents/{doc_id}")
def ws_edit(doc_id: int, req: WsDocEdit, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _mine(db, user, doc_id)
    if _ws(d) == "pending":
        raise HTTPException(400, "Withdraw it from approval before editing")
    if req.title and req.title.strip():
        d.title = req.title.strip()
    if req.content is not None and len(req.content.strip()) >= 10:
        d.content = req.content
        _set_chunks(db, d, req.content)
    db.commit()
    return {"ok": True}


@router.post("/workspace/documents/{doc_id}/submit")
def ws_submit(doc_id: int, req: SubmitIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _mine(db, user, doc_id)
    if _ws(d) != "personal":
        raise HTTPException(400, "Already submitted")
    org = db.query(models.Organization).get(user.org_id)
    depts = [x.strip() for x in (org.departments or "").split(",") if x.strip()]
    if req.department != "All" and req.department not in depts:
        raise HTTPException(400, f"Unknown department '{req.department}'")
    if req.visibility not in VISIBILITIES:
        raise HTTPException(400, "Invalid classification")
    approvers = _approvers(db, user.org_id, req.department, exclude_id=user.id)
    if not approvers and user.role != "owner":
        raise HTTPException(400, "There is nobody who can approve this department's documents yet")
    d.workspace, d.submit_department, d.submit_visibility = "pending", req.department, req.visibility
    d.submit_note, d.submitted_at, d.review_note = req.note.strip(), _now(), None
    for a in approvers:
        _notify(db, user.org_id, user.id, a.id, "review_request",
                f"{user.name} submitted '{d.title}' for approval ({req.department}, {req.visibility})", document_id=d.id)
    _log(db, user.org_id, user.id, "document_submitted", f"'{d.title}' -> {req.department}/{req.visibility}")
    db.commit()
    return {"ok": True, "waiting_on": [a.name for a in approvers]}


@router.post("/workspace/documents/{doc_id}/withdraw")
def ws_withdraw(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    d = _mine(db, user, doc_id)
    if _ws(d) != "pending":
        raise HTTPException(400, "Not waiting for approval")
    d.workspace = "personal"
    _log(db, user.org_id, user.id, "document_withdrawn", d.title)
    db.commit()
    return {"ok": True}


@router.post("/notes")
def create_note(req: NoteIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if req.kind not in ("note", "checklist"):
        raise HTTPException(400, "kind must be note or checklist")
    n = models.Note(org_id=user.org_id, user_id=user.id, kind=req.kind, title=req.title.strip(), body=req.body)
    db.add(n)
    db.commit()
    return _note_out(n)


@router.put("/notes/{nid}")
def update_note(nid: int, req: NoteIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    n = db.query(models.Note).get(nid)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Note not found")
    n.title, n.body, n.updated_at = req.title.strip(), req.body, _now()
    db.commit()
    return _note_out(n)


@router.delete("/notes/{nid}")
def delete_note(nid: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    n = db.query(models.Note).get(nid)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Note not found")
    db.delete(n)
    db.commit()
    return {"ok": True}


def _can_approve(user, d) -> bool:
    if _ws(d) != "pending" or d.deleted_at is not None:
        return False
    r = auth.rank(user)
    if r >= ADMIN:
        return True
    return r >= MANAGER and d.submit_department == user.department


@router.get("/approvals")
def approvals(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    names = _names(db, user.org_id)
    pend = [d for d in db.query(models.Document).filter_by(org_id=user.org_id, workspace="pending")
            if _can_approve(user, d) and (d.uploaded_by != user.id or user.role == "owner")]
    pend.sort(key=lambda d: d.submitted_at or d.created_at)
    return {"documents": [{"id": d.id, "title": d.title, "author": names.get(d.uploaded_by),
                           "department": d.submit_department, "visibility": d.submit_visibility,
                           "note": d.submit_note, "submitted_at": _iso(d.submitted_at), "file_type": d.file_type,
                           "preview": " ".join((d.content or "").split())[:260]} for d in pend],
            "dual_control": [x for x in list_dual(user, db) if x["status"] == "pending"]}


@router.get("/approvals/{doc_id}")
def approval_detail(doc_id: int, user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    d = _doc(db, user, doc_id)
    if not _can_approve(user, d):
        raise HTTPException(404, "Not in your approval queue")
    _log(db, user.org_id, user.id, "approval_review_opened", f"'{d.title}'")
    db.commit()
    return {"id": d.id, "title": d.title, "content": d.content, "author": _names(db, user.org_id).get(d.uploaded_by),
            "department": d.submit_department, "visibility": d.submit_visibility, "note": d.submit_note}


@router.post("/approvals/{doc_id}/decide")
def approval_decide(doc_id: int, req: ApprovalDecide, user: models.User = Depends(auth.require_role("manager")),
                    db: Session = Depends(get_db)):
    d = _doc(db, user, doc_id)
    if not _can_approve(user, d):
        raise HTTPException(404, "Not in your approval queue")
    if d.uploaded_by == user.id and user.role != "owner":
        raise HTTPException(403, "You can't approve your own submission")
    if req.decision not in ("approve", "reject", "changes"):
        raise HTTPException(400, "decision must be approve, reject or changes")
    note = req.note.strip()
    if req.decision in ("reject", "changes") and not note:
        raise HTTPException(400, "Please tell the author why")
    if req.decision == "approve":
        dept = req.department or d.submit_department or "All"
        vis = req.visibility or d.submit_visibility or "internal"
        if vis not in VISIBILITIES:
            raise HTTPException(400, "Invalid classification")
        if dept != "All" and auth.rank(user) < ADMIN and dept != user.department:
            raise HTTPException(403, "You can only approve into your own department")
        if dept == "All" and auth.rank(user) < ADMIN:
            raise HTTPException(403, "Company-wide documents need an admin or owner to approve")
        d.workspace, d.department, d.visibility = "company", dept, vis
        d.owner_id = d.uploaded_by
        d.last_reviewed_at, d.last_reviewed_by = _now(), user.id
        d.review_note = None
        if vis == "restricted":
            d.allowed_user_ids = ",".join(str(i) for i in sorted({d.uploaded_by, user.id}))
        msg = f"'{d.title}' was approved and is now an official {vis} document ({dept})"
    else:
        d.workspace = "personal"
        d.review_note = ("rejected: " if req.decision == "reject" else "changes requested: ") + note
        msg = f"'{d.title}' was " + ("rejected" if req.decision == "reject" else "sent back for changes") + f": {note}"
    _notify(db, user.org_id, user.id, d.uploaded_by, "review_result", msg + f" - by {user.name}", document_id=d.id)
    _log(db, user.org_id, user.id, "approval_" + req.decision, f"'{d.title}'")
    db.commit()
    return {"ok": True, "decision": req.decision}


# ------------------------------------------------------------- help chatbot ---

class HelpIn(BaseModel):
    question: str = Field(max_length=400)


@router.post("/help/ask")
def help_ask(req: HelpIn, user: models.User = Depends(auth.get_current_user)):
    """NOTE: no database session and no document code is even imported by help_bot.
    This endpoint cannot read company knowledge - by construction, not by promise."""
    return help_bot.answer(req.question)
