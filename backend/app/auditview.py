"""Audit log viewer: categories, filters, search and a detail view on top of the existing audit_log table.

Honest description: this is an APPEND-ONLY APPLICATION AUDIT LOG. The application never edits or deletes rows,
but it is not cryptographically tamper-proof (someone with direct database access could change it).
Manager role or above only - same rule as the original /audit-log endpoint, which is left untouched.
"""
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models, auth
from .db import get_db

router = APIRouter()

# category -> (exact action names, prefixes)
CATEGORIES = {
    "Authentication": ({"login", "login_failed", "login_blocked", "password_changed", "password_reset"}, ()),
    "Access & permissions": ({"permanent_access", "jit_created", "jit_redeemed", "dual_control_requested",
                              "classification_changed", "classify", "employee_approved"},
                             ("access_", "doc_access_", "approval_")),
    "Document changes": ({"document_upload", "document_edit", "document_submitted", "document_withdrawn",
                          "document_deleted", "document_restored", "document_purged", "document_purged_by_admin",
                          "workspace_upload", "update_requested", "update_sent", "folder_created", "folder_deleted",
                          "folder_add", "document_view"}, ()),
    "Rollbacks": ({"document_rollback"}, ()),
    "Knowledge changes": ({"fact_added", "impact_fact", "conflict_proposed", "conflict_resolved"}, ()),
    "Answer generation": ({"query", "search", "ai_explain"}, ()),
    "Verification": ({"document_verified", "knowledge_verified", "knowledge_rejected", "answer_reviewed",
                      "answer_flagged", "impact_still_valid", "impact_replaced", "review_escalated",
                      "approval_review_opened"}, ()),
    "Escalation": ({"knowledge_request", "knowledge_answered", "knowledge_declined", "knowledge_forwarded"}, ()),
    "Administration": ({"org_created", "org_settings_updated", "employee_created", "employee_updated",
                        "task_created", "task_completed"}, ()),
}
ORDER = list(CATEGORIES) + ["Other"]

LABELS = {
    "login": "signed in", "login_failed": "failed to sign in", "login_blocked": "was temporarily locked out after repeated failed sign-ins",
    "password_changed": "changed their password", "password_reset": "had a password reset",
    "query": "asked a question", "search": "searched", "ai_explain": "asked the local model to explain an answer",
    "document_upload": "uploaded a document", "document_edit": "edited a document", "document_view": "opened a protected document",
    "document_rollback": "rolled a document back to an earlier version (history kept)",
    "document_verified": "verified a document", "document_deleted": "moved a document to the recycle bin",
    "document_restored": "restored a document", "document_submitted": "submitted a document for approval",
    "knowledge_request": "asked the organization for help", "knowledge_answered": "answered a knowledge request",
    "knowledge_declined": "declined a knowledge request", "knowledge_forwarded": "forwarded a knowledge request",
    "knowledge_verified": "verified a human answer (now organizational memory)", "knowledge_rejected": "did not verify a human answer",
    "impact_replaced": "replaced an answer after a knowledge change", "impact_still_valid": "confirmed an answer is still valid after a knowledge change",
    "answer_flagged": "flagged an answer as wrong", "answer_reviewed": "reviewed an answer",
    "access_requested": "requested access", "access_granted": "granted access", "permanent_access": "gave permanent access",
    "jit_created": "created a one-time access link", "jit_redeemed": "used a one-time access link",
    "dual_control_requested": "asked for a second approval", "classification_changed": "changed a document's visibility",
    "fact_added": "added a fact to the knowledge graph", "conflict_proposed": "proposed a conflict resolution", "conflict_resolved": "resolved a conflict",
}


def category_of(action: str) -> str:
    a = action or ""
    for cat, (exact, prefixes) in CATEGORIES.items():
        if a in exact or any(a.startswith(p) for p in prefixes):
            return cat
    return "Other"


def label_of(action: str) -> str:
    return LABELS.get(action) or (action or "").replace("_", " ")


def _iso(dt):
    return dt.isoformat() if dt else None


def _day(s: Optional[str], end=False, tz_offset=0):
    """Start (or end) of the given calendar day IN THE VIEWER'S TIMEZONE, converted to UTC (timestamps are stored in UTC).
    tz_offset is JavaScript's getTimezoneOffset(): minutes UTC is ahead of local time (India = -330)."""
    if not s:
        return None
    try:
        d = datetime.datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-10-01")
    d = d + datetime.timedelta(minutes=tz_offset)
    return d + datetime.timedelta(days=1) if end else d


def _row(r, names, roles):
    return {"id": r.id, "action": r.action, "category": category_of(r.action), "summary": label_of(r.action),
            "detail": r.detail or "", "actor": names.get(r.user_id, "system"), "actor_id": r.user_id,
            "actor_role": roles.get(r.user_id), "created_at": _iso(r.created_at)}


@router.get("/audit/events")
def audit_events(category: Optional[str] = None, q: str = "", actor_id: Optional[int] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 tz_offset: int = Query(0, ge=-900, le=900),
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    users = db.query(models.User).filter_by(org_id=user.org_id).all()
    names, roles = {u.id: u.name for u in users}, {u.id: u.role for u in users}
    base = db.query(models.AuditLog).filter(models.AuditLog.org_id == user.org_id)
    if actor_id is not None:
        base = base.filter(models.AuditLog.user_id == actor_id)
    df, dt = _day(date_from, tz_offset=tz_offset), _day(date_to, end=True, tz_offset=tz_offset)
    if df:
        base = base.filter(models.AuditLog.created_at >= df)
    if dt:
        base = base.filter(models.AuditLog.created_at < dt)
    term = q.strip().lower()
    if term:
        ids = [uid for uid, n in names.items() if term in n.lower()]
        conds = [models.AuditLog.action.ilike(f"%{term}%"), models.AuditLog.detail.ilike(f"%{term}%")]
        if ids:
            conds.append(models.AuditLog.user_id.in_(ids))
        base = base.filter(or_(*conds))
    rows = base.order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc()).all()
    counts = {c: 0 for c in ORDER}
    for r in rows:
        counts[category_of(r.action)] += 1
    if category:
        if category not in ORDER:
            raise HTTPException(400, "Unknown category")
        rows = [r for r in rows if category_of(r.action) == category]
    page = rows[offset:offset + limit]
    return {"total": len(rows), "offset": offset, "limit": limit, "events": [_row(r, names, roles) for r in page],
            "categories": [{"name": c, "count": counts[c]} for c in ORDER if counts[c] or c == category],
            "actors": sorted([{"id": u.id, "name": u.name} for u in users], key=lambda a: a["name"].lower()),
            "note": "Append-only, hash-chained audit log. Use Verify integrity to check that no row was edited or removed."}


@router.get("/audit/events/{event_id}")
def audit_event(event_id: int, user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    r = db.get(models.AuditLog, event_id)
    if not r or r.org_id != user.org_id:
        raise HTTPException(404, "Event not found")
    users = db.query(models.User).filter_by(org_id=user.org_id).all()
    names, roles = {u.id: u.name for u in users}, {u.id: u.role for u in users}
    related = []
    if (r.detail or "").strip():                        # other events about the same thing (same detail text)
        related = db.query(models.AuditLog).filter(
            models.AuditLog.org_id == user.org_id, models.AuditLog.id != r.id,
            models.AuditLog.detail == r.detail).order_by(models.AuditLog.created_at.desc()).limit(10).all()
    around = db.query(models.AuditLog).filter(
        models.AuditLog.org_id == user.org_id, models.AuditLog.user_id == r.user_id, models.AuditLog.id != r.id,
        models.AuditLog.created_at >= r.created_at - datetime.timedelta(minutes=5),
        models.AuditLog.created_at <= r.created_at + datetime.timedelta(minutes=5)).order_by(
        models.AuditLog.created_at).limit(10).all() if r.user_id else []
    return {"event": _row(r, names, roles), "raw": {"action": r.action, "detail": r.detail},
            "same_subject": [_row(x, names, roles) for x in related],
            "same_actor_within_5_minutes": [_row(x, names, roles) for x in around]}
