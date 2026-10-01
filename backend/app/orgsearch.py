"""Permission-aware knowledge search with filters.

Order of operations (same rule as Ask): 1) permission gate  2) filters  3) ranking.
Restricted / out-of-department documents are dropped on the SERVER before anything is matched, counted or
returned - including the filter option lists - so nothing about them can be discovered from the API.
Matching = Anamnesis' own stem + synonym/concept analysis (app/nlp.py, synonyms.json); not embeddings.
"""
import datetime
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from . import models, auth, retrieval, nlp
from .db import get_db

router = APIRouter()


def _tags(d):
    return [t.strip() for t in (d.tags or "").split(",") if t.strip()]


def _date(s, end=False, tz_offset=0):
    if not s:
        return None
    try:
        dt = datetime.datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Bad date '{s}', use YYYY-MM-DD")
    dt = dt + datetime.timedelta(minutes=tz_offset)      # the viewer's calendar day, converted to UTC (timestamps are UTC)
    return dt + datetime.timedelta(days=1) if end else dt


def _snippet(content, stems):
    for sent in re.split(r"(?<=[.!?])\s+|\n+", content or ""):
        if sent.strip() and stems & nlp.analyze(sent)[0]:
            return sent.strip()[:240]
    return " ".join((content or "").split())[:240]


@router.get("/search")
def search_knowledge(q: str = "", department: Optional[str] = None, tag: Optional[str] = None,
                     owner_id: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                     visibility: Optional[str] = None, limit: int = Query(25, ge=1, le=100),
                     tz_offset: int = Query(0, ge=-900, le=900),
                     user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    # 1) PERMISSION GATE FIRST - everything below only ever sees documents this user may open
    docs = [d for d in db.query(models.Document).filter(models.Document.org_id == user.org_id)
            if (d.workspace or "company") == "company" and retrieval.user_can_see_document(user, d, db)]
    names = {u.id: u.name for u in db.query(models.User).filter(models.User.org_id == user.org_id)}
    facets = {"departments": sorted({d.department for d in docs if d.department}),
              "tags": sorted({t for d in docs for t in _tags(d)}, key=str.lower),
              "owners": sorted([{"id": i, "name": names.get(i)} for i in {(d.owner_id or d.uploaded_by) for d in docs}
                                if names.get(i)], key=lambda o: o["name"].lower(), ),
              "visibilities": sorted({d.visibility for d in docs})}
    # 2) FILTERS
    df, dt = _date(date_from, tz_offset=tz_offset), _date(date_to, end=True, tz_offset=tz_offset)
    if department:
        docs = [d for d in docs if (d.department or "").lower() == department.lower()]
    if tag:
        docs = [d for d in docs if tag.lower() in [t.lower() for t in _tags(d)]]
    if owner_id is not None:
        docs = [d for d in docs if (d.owner_id or d.uploaded_by) == owner_id]
    if visibility:
        docs = [d for d in docs if d.visibility == visibility.lower()]
    if df:
        docs = [d for d in docs if d.created_at and d.created_at >= df]
    if dt:
        docs = [d for d in docs if d.created_at and d.created_at < dt]
    # 3) RANKING (only when there is a query)
    rows = []
    q = (q or "").strip()
    if q:
        qunits, qstems = nlp.analyze(q)[0], nlp.analyze(q)[1]
        for d in docs:
            tu, cu = nlp.analyze(d.title)[0], nlp.analyze(d.content or "")[0]
            hit_t, hit_c = len(qunits & tu), len(qunits & cu)
            if not (hit_t or hit_c):
                continue
            rows.append((3 * hit_t + hit_c / max(1, len(qunits)), d))
        rows.sort(key=lambda r: (-r[0], r[1].title))
    else:
        rows = sorted(((0, d) for d in docs), key=lambda r: r[1].created_at or datetime.datetime.min, reverse=True)
    out = [{"id": d.id, "title": d.title, "department": d.department, "visibility": d.visibility, "tags": _tags(d),
            "owner": names.get(d.owner_id or d.uploaded_by), "owner_id": d.owner_id or d.uploaded_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "snippet": _snippet(d.content, nlp.analyze(q)[0]) if q else " ".join((d.content or "").split())[:240],
            "score": round(s, 2)} for s, d in rows[:limit]]
    db.add(models.AuditLog(org_id=user.org_id, user_id=user.id, action="search",
                           detail=f"q={q[:80]!r} filters={{dept:{department},tag:{tag},owner:{owner_id}}} results={len(rows)}"))
    db.commit()
    return {"query": q, "total": len(rows), "results": out, "facets": facets}
