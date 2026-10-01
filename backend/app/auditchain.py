"""Tamper-evident audit log: every audit row stores the SHA-256 of its own content plus the previous row's hash
(one chain per organization). Editing or deleting an old row breaks every hash after it, and GET /audit/verify
shows exactly where. Also: CSV export of the filtered log.

Head anchor: every successful verify saves the newest hash and the row count to audit_anchor.json, a file OUTSIDE
the database. The next verify fails if that head is no longer where it was, so deleting only the newest rows is now
detected. You can also paste a head hash you noted earlier (GET /audit/verify?noted_head=...).

Honest limits: someone with full database access could recompute the whole chain, and someone who can also edit
or delete audit_anchor.json can reset the anchor, so keep a copy of the head hash somewhere off the server (notes,
a screenshot). Rows written before this feature existed have no hash and are reported as 'unprotected', not hidden.
"""
import csv
import datetime
import hashlib
import io
import json
import os
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
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the backend folder
ANCHOR_FILE = os.environ.get("ANAMNESIS_AUDIT_ANCHOR") or os.path.join(_APP_DIR, "audit_anchor.json")
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


def _load_anchors():
    try:
        with open(ANCHOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None                       # unreadable or corrupt: report it, never silently replace it


def _save_anchors(data):
    tmp = ANCHOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ANCHOR_FILE)


def _check_anchor(org_id, hashes):
    """Compare the chain with the head saved by an earlier verify. Returns (anchor_info, failure_reason or None)."""
    key = str(org_id)
    anchors = _load_anchors()
    if anchors is None:
        return {"status": "unavailable", "detail": "audit_anchor.json is unreadable; fix or delete it"}, None
    old = anchors.get(key)
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if old:
        n, head = int(old.get("count", 0)), old.get("head")
        if len(hashes) < n or (n and hashes[n - 1] != head):
            return {"status": "mismatch", "saved_rows": n, "saved_at": old.get("saved_at"), "current_rows": len(hashes)}, (
                f"The head saved on {old.get('saved_at', 'an earlier check')} ({n} rows) is no longer in the chain: "
                "the newest rows were removed or the chain was rebuilt. If you deliberately reset the database, "
                "delete audit_anchor.json in the backend folder and verify again")
        if len(hashes) == n:
            return {"status": "matches", "saved_rows": n, "saved_at": old.get("saved_at"), "new_rows": 0}, None
    if hashes:
        anchors[key] = {"count": len(hashes), "head": hashes[-1], "saved_at": now}
        try:
            _save_anchors(anchors)
        except OSError:
            return {"status": "unavailable", "detail": "could not write audit_anchor.json"}, None
    status = "advanced" if old else "created"
    return {"status": status, "saved_rows": len(hashes), "saved_at": now,
            "new_rows": len(hashes) - int(old.get("count", 0)) if old else None}, None


@router.get("/audit/verify")
def verify(noted_head: Optional[str] = Query(None, max_length=64),
           user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    rows = db.query(models.AuditLog).filter(models.AuditLog.org_id == user.org_id).order_by(models.AuditLog.id).all()
    legacy = sum(1 for r in rows if r.row_hash is None and not _after_chain_start(rows, r))
    prev, checked, started, hashes = "", 0, False, []
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
        hashes.append(r.row_hash)
    noted = (noted_head or "").strip().lower()
    noted_info = None
    if noted:
        if len(noted) < 12 or any(c not in "0123456789abcdef" for c in noted):
            raise HTTPException(400, "Noted head hash must be at least 12 hex characters")
        hit = next((i for i, h in enumerate(hashes) if h.startswith(noted)), None)
        if hit is None:
            return _bad(None, checked, legacy, "The head hash you noted is not in the chain: the newest rows were "
                        "removed, or the chain was rebuilt after you noted it")
        noted_info = {"found_at_row": hit + 1, "rows_after_it": len(hashes) - hit - 1}
    anchor, why = _check_anchor(user.org_id, hashes)
    if why:
        return {"ok": False, "checked": checked, "unprotected_older_rows": legacy, "broken_at": None, "reason": why,
                "head": None, "head_full": None, "anchor": anchor, "noted": None}
    return {"ok": True, "checked": checked, "unprotected_older_rows": legacy, "broken_at": None, "reason": None,
            "head": prev[:16] if prev else None, "head_full": prev or None, "anchor": anchor, "noted": noted_info}


def _after_chain_start(rows, r):
    for x in rows:
        if x.row_hash is not None:
            return x.id < r.id
    return False


def _bad(r, checked, legacy, why):
    return {"ok": False, "checked": checked, "unprotected_older_rows": legacy, "broken_at": r.id if r is not None else None,
            "reason": why, "head": None, "head_full": None, "anchor": None, "noted": None}


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
