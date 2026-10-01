"""Tamper-evident audit log: every audit row stores the SHA-256 of its own content plus the previous row's hash
(one chain per organization). Editing or deleting an old row breaks every hash after it, and GET /audit/verify
shows exactly where. Also: CSV export of the filtered log.

Honest limits: someone with full database access could recompute the whole chain, and removing the very last
rows cannot be detected unless you have noted the head hash earlier. Rows written before this feature existed
have no hash and are reported as 'unprotected', not hidden.
"""
import csv
import datetime
import hashlib
import io
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import event, or_, text as sql_text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import instance_state

from . import models, auth
from .auditview import _day, category_of, label_of, ORDER
from .db import get_db

router = APIRouter()
_LOCK = threading.Lock()          # keeps two requests from writing the same "previous hash" at the same moment


def ensure_schema(engine):
    with engine.begin() as con:
        existing = {r[1] for r in con.execute(sql_text("PRAGMA table_info(audit_log)"))}
        for name in ("prev_hash", "row_hash"):
            if existing and name not in existing:
                con.execute(sql_text(f"ALTER TABLE audit_log ADD COLUMN {name} TEXT"))


def digest(prev, org_id, user_id, action, detail, created_at):
    parts = [prev or "", str(org_id), str(user_id), action or "", detail or "",
             created_at.isoformat() if created_at else ""]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@event.listens_for(Session, "before_flush")
def _chain_new_rows(session, flush_context, instances):
    new = [o for o in session.new if isinstance(o, models.AuditLog)]
    if not new:
        return
    if not session.info.get("_chain_lock") and _LOCK.acquire(timeout=3):
        session.info["_chain_lock"] = True            # released when this session's transaction ends
    new.sort(key=lambda o: instance_state(o).insert_order or 0)
    last = {}
    with session.no_autoflush:
        for o in new:
            if o.created_at is None:
                o.created_at = datetime.datetime.utcnow()
            if o.org_id not in last:
                row = session.query(models.AuditLog.row_hash).filter(
                    models.AuditLog.org_id == o.org_id, models.AuditLog.row_hash.isnot(None)
                ).order_by(models.AuditLog.id.desc()).first()
                last[o.org_id] = row[0] if row else ""
            o.prev_hash = last[o.org_id]
            o.row_hash = digest(o.prev_hash, o.org_id, o.user_id, o.action, o.detail, o.created_at)
            last[o.org_id] = o.row_hash


@event.listens_for(Session, "after_transaction_end")
def _release(session, transaction):
    if transaction.parent is None and session.info.pop("_chain_lock", False):
        try:
            _LOCK.release()
        except RuntimeError:
            pass


@router.get("/audit/verify")
def verify(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    rows = db.query(models.AuditLog).filter(models.AuditLog.org_id == user.org_id).order_by(models.AuditLog.id).all()
    legacy = sum(1 for r in rows if r.row_hash is None and not _after_chain_start(rows, r))
    prev, checked, started = "", 0, False
    for r in rows:
        if r.row_hash is None:
            if started:
                return _bad(r, checked, legacy, "A row inside the protected chain has no hash")
            continue
        started = True
        if (r.prev_hash or "") != prev:
            return _bad(r, checked, legacy, "The link to the previous row does not match (a row was removed, added or reordered)")
        if digest(r.prev_hash, r.org_id, r.user_id, r.action, r.detail, r.created_at) != r.row_hash:
            return _bad(r, checked, legacy, "This row's content no longer matches its hash (it was edited)")
        prev, checked = r.row_hash, checked + 1
    return {"ok": True, "checked": checked, "unprotected_older_rows": legacy, "broken_at": None, "reason": None,
            "head": prev[:16] if prev else None}


def _after_chain_start(rows, r):
    for x in rows:
        if x.row_hash is not None:
            return x.id < r.id
    return False


def _bad(r, checked, legacy, why):
    return {"ok": False, "checked": checked, "unprotected_older_rows": legacy, "broken_at": r.id, "reason": why, "head": None}


def _safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s      # stops spreadsheet formula injection


@router.get("/audit/export.csv")
def export_csv(category: Optional[str] = None, q: str = "", actor_id: Optional[int] = None,
               date_from: Optional[str] = None, date_to: Optional[str] = None,
               tz_offset: int = Query(0, ge=-900, le=900),
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
    if category and category not in ORDER:
        raise HTTPException(400, "Unknown category")
    rows = base.order_by(models.AuditLog.id.desc()).limit(5000).all()
    if category:
        rows = [r for r in rows if category_of(r.action) == category]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "time_utc", "category", "actor", "actor_role", "action", "summary", "detail", "row_hash"])
    for r in rows:
        w.writerow([r.id, r.created_at.isoformat() if r.created_at else "", category_of(r.action),
                    _safe(names.get(r.user_id, "system")), roles.get(r.user_id, ""), r.action, label_of(r.action),
                    _safe(r.detail), r.row_hash or ""])
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="audit-log-{stamp}.csv"'})
