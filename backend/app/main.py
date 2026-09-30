import os
import json
import re
import secrets
import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import models, schemas, auth, retrieval, graph, extract, features, insights, impact, knowledge, llm
from .db import engine, get_db, SessionLocal

models.Base.metadata.create_all(bind=engine)
features.ensure_schema(engine)      # adds any missing column to an older anamnesis.db automatically
insights.ensure_schema(engine)
impact.ensure_schema(engine)        # knowledge-impact columns on qa_records

CHUNK_VERSION = "3"


def reindex_if_needed():
    """Old documents were split by an older chunker. Re-split them once (content is untouched)."""
    marker = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".chunk_version")
    try:
        if os.path.exists(marker) and open(marker).read().strip() == CHUNK_VERSION:
            return
        db = SessionLocal()
        try:
            for d in db.query(models.Document).all():
                db.query(models.Chunk).filter_by(document_id=d.id).delete()
                for i, c in enumerate(retrieval.chunk_text(d.content)):
                    db.add(models.Chunk(document_id=d.id, org_id=d.org_id, text=c, order_index=i))
            db.commit()
        finally:
            db.close()
        with open(marker, "w") as f:
            f.write(CHUNK_VERSION)
    except Exception as e:      # never stop the server from starting over an index refresh
        print("reindex skipped:", e)


reindex_if_needed()


def _startup_purge():
    """Documents that sat in the recycle bin for more than 30 days are removed for good."""
    db = SessionLocal()
    try:
        features.purge_expired(db)
    except Exception as e:
        print("recycle-bin purge skipped:", e)
    finally:
        db.close()


_startup_purge()
insights.run_all_escalations()      # move overdue flags / review requests up the reporting line

app = FastAPI(title="Anamnesis API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

ROLES = ["guest", "intern", "member", "manager", "admin", "owner"]
VISIBILITIES = ("public", "internal", "confidential", "restricted")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "allowed_emails.txt")


def log(db: Session, org_id, user_id, action: str, detail: str = ""):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action, detail=detail))
    db.commit()


def sync_allowed_emails():
    """Founder allow-list: allowed_emails.txt (one per line) + ANAMNESIS_ALLOWED_EMAILS env."""
    emails = set()
    if os.path.exists(ALLOWED_FILE):
        for line in open(ALLOWED_FILE, encoding="utf-8"):
            line = line.strip().lower()
            if line and not line.startswith("#"):
                emails.add(line)
    for e in os.environ.get("ANAMNESIS_ALLOWED_EMAILS", "").split(","):
        if e.strip():
            emails.add(e.strip().lower())
    db = SessionLocal()
    try:
        for e in emails:
            if not db.query(models.AllowedEmail).filter_by(email=e).first():
                db.add(models.AllowedEmail(email=e))
        db.commit()
    finally:
        db.close()


sync_allowed_emails()


def make_username(db: Session, name: str) -> str:
    base = re.sub(r"[^a-z0-9.]+", "", name.strip().lower().replace(" ", ".")) or "user"
    cand, n = base, 1
    while db.query(models.User).filter_by(username=cand).first():
        n += 1
        cand = f"{base}{n}"
    return cand


def temp_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def org_departments(org: models.Organization) -> List[str]:
    return [d.strip() for d in (org.departments or "").split(",") if d.strip()]


def supervisor_for(db: Session, user: models.User) -> Optional[models.User]:
    """Who reviews this person's flagged answers / receives their updates."""
    if user.supervisor_id:
        s = db.query(models.User).get(user.supervisor_id)
        if s and s.active:
            return s
    mgr = db.query(models.User).filter(
        models.User.org_id == user.org_id, models.User.department == user.department,
        models.User.role == "manager", models.User.active == True, models.User.id != user.id  # noqa: E712
    ).first()
    if mgr:
        return mgr
    return db.query(models.User).filter(
        models.User.org_id == user.org_id, models.User.role.in_(["admin", "owner"]),
        models.User.id != user.id).first()


def notify(db: Session, org_id, from_id, to_id, kind, text, document_id=None, qa_id=None):
    if to_id and to_id != from_id:
        db.add(models.WorkUpdate(org_id=org_id, from_user_id=from_id, to_user_id=to_id,
                                 kind=kind, text=text, document_id=document_id, qa_id=qa_id))


def user_out(u: models.User, detail: bool):
    d = {"id": u.id, "name": u.name, "role": u.role, "department": u.department,
         "supervisor_id": u.supervisor_id}
    if detail:
        d.update({"username": u.username, "email": u.email, "active": u.active,
                  "must_change_password": u.must_change_password})
    return d


# ---------------------------------------------------------------- auth -----

@app.post("/auth/signup", response_model=schemas.TokenResponse)
def signup(req: schemas.SignupRequest, db: Session = Depends(get_db)):
    """Create an organization. Only pre-approved (allow-listed) emails can do this,
    and each approved email can create exactly one org. Employees never self-register:
    the owner/admin creates them under People."""
    email = req.email.lower()
    allowed = db.query(models.AllowedEmail).filter_by(email=email).first()
    if not allowed:
        raise HTTPException(403, "This email is not approved to create an organization. "
                                 "Ask the Anamnesis team to add it to the founder allow-list.")
    if allowed.used or db.query(models.User).filter_by(email=email).first():
        raise HTTPException(400, "This email has already been used to create an organization")

    org = models.Organization(name=req.org_name.strip())
    db.add(org)
    db.flush()
    user = models.User(org_id=org.id, name=req.name.strip(), email=email,
                       username=make_username(db, req.name), password_hash=auth.hash_password(req.password),
                       role="owner", department="Management")
    org.departments = "Management," + org.departments
    allowed.used = True
    db.add(user)
    db.commit()
    db.refresh(user)
    log(db, org.id, user.id, "org_created", org.name)
    return schemas.TokenResponse(access_token=auth.create_access_token(user.id))


@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(req: schemas.LoginRequest, db: Session = Depends(get_db)):
    ident = req.identifier.strip().lower()
    user = db.query(models.User).filter(
        (models.User.email == ident) | (models.User.username == ident)).first()
    if not user or not user.active or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Incorrect username/email or password")
    if user.pending_approval:
        raise HTTPException(403, "Your account is waiting on approval from an admin or owner")
    log(db, user.org_id, user.id, "login")
    return schemas.TokenResponse(access_token=auth.create_access_token(user.id),
                                 must_change_password=bool(user.must_change_password))


@app.post("/auth/change-password")
def change_password(req: schemas.ChangePassword, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    if not auth.verify_password(req.old_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = auth.hash_password(req.new_password)
    user.must_change_password = False
    db.commit()
    log(db, user.org_id, user.id, "password_changed")
    return {"ok": True}


@app.get("/me")
def me(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    org = db.query(models.Organization).get(user.org_id)
    sup = supervisor_for(db, user)
    return {"id": user.id, "name": user.name, "username": user.username, "email": user.email,
            "role": user.role, "department": user.department, "org_id": org.id,
            "org_name": org.name, "accent": org.accent, "departments": org_departments(org),
            "supervisor": sup.name if sup else None,
            "must_change_password": bool(user.must_change_password),
            "member_since": user.created_at.isoformat()}


@app.patch("/me")
def update_me(req: schemas.ProfileUpdate, user: models.User = Depends(auth.get_current_user),
              db: Session = Depends(get_db)):
    user.name = req.name.strip() or user.name
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------- org / people -----

@app.get("/org/settings")
def get_settings(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    org = db.query(models.Organization).get(user.org_id)
    return {"name": org.name, "accent": org.accent, "departments": org_departments(org)}


@app.patch("/org/settings")
def update_settings(req: schemas.OrgSettings, user: models.User = Depends(auth.require_role("admin")),
                    db: Session = Depends(get_db)):
    org = db.query(models.Organization).get(user.org_id)
    if req.name:
        org.name = req.name.strip()
    if req.accent and re.fullmatch(r"#[0-9a-fA-F]{6}", req.accent):
        org.accent = req.accent
    if req.departments is not None:
        depts = [d.strip() for d in req.departments if d.strip()]
        if not depts:
            raise HTTPException(400, "Keep at least one department")
        org.departments = ",".join(dict.fromkeys(depts))
    db.commit()
    log(db, user.org_id, user.id, "org_settings_updated")
    return {"ok": True}


def _check_supervisor(db, org_id, sup_id):
    """A supervisor must exist, be active, be in the same org, and be a manager or above."""
    if not sup_id:
        return None
    sup = db.query(models.User).get(sup_id)
    if not sup or sup.org_id != org_id or not sup.active or auth.rank(sup) < auth.ROLE_RANK["manager"]:
        raise HTTPException(400, "Supervisor must be an active manager or above in your organization")
    return sup


@app.get("/org/employees")
def list_employees(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    people = db.query(models.User).filter(models.User.org_id == user.org_id).all()
    detail = auth.rank(user) >= auth.ROLE_RANK["manager"]
    return [user_out(p, detail) for p in people]


@app.post("/org/employees")
def create_employee(req: schemas.EmployeeCreate, user: models.User = Depends(auth.require_role("manager")),
                    db: Session = Depends(get_db)):
    """Owner/admin/manager creates an employee; the system generates the username and
    a one-time temporary password (shown once, must be changed on first login)."""
    if req.role not in ROLES:
        raise HTTPException(400, "Unknown role")
    if auth.ROLE_RANK[req.role] >= auth.rank(user):
        raise HTTPException(403, "You can only create people below your own role")
    org = db.query(models.Organization).get(user.org_id)
    dept = req.department
    if user.role == "manager":
        dept = user.department  # managers only add to their own department
    if dept not in org_departments(org):
        raise HTTPException(400, f"Unknown department '{dept}'")
    _check_supervisor(db, user.org_id, req.supervisor_id)
    email = req.email.lower() if req.email else None
    if email and db.query(models.User).filter_by(email=email).first():
        raise HTTPException(400, "That email is already in use")

    pwd = temp_password()
    needs_approval = user.role == "manager"  # admins/owners sit at the top of the chain already
    emp = models.User(org_id=user.org_id, name=req.name.strip(), email=email,
                      username=make_username(db, req.name), password_hash=auth.hash_password(pwd),
                      role=req.role, department=dept, must_change_password=True,
                      pending_approval=needs_approval,
                      supervisor_id=req.supervisor_id or (user.id if user.role == "manager" else None))
    db.add(emp)
    db.commit()
    db.refresh(emp)
    if not emp.supervisor_id:
        sup = supervisor_for(db, emp)
        emp.supervisor_id = sup.id if sup else user.id
        db.commit()
    log(db, user.org_id, user.id, "employee_created", f"{emp.name} ({emp.role}, {emp.department})"
        + (" - pending admin approval" if needs_approval else ""))
    return {"id": emp.id, "name": emp.name, "username": emp.username, "temp_password": pwd,
            "role": emp.role, "department": emp.department, "pending_approval": needs_approval}


def dept_head(db: Session, org_id: int, department: str) -> Optional[models.User]:
    """The person who owns access decisions for a department: its manager, or the
    org owner if the department has none."""
    mgr = db.query(models.User).filter(
        models.User.org_id == org_id, models.User.department == department,
        models.User.role.in_(["manager", "admin"]), models.User.active == True,  # noqa: E712
    ).first()
    if mgr:
        return mgr
    return db.query(models.User).filter_by(org_id=org_id, role="owner").first()


@app.get("/org/employees/pending")
def pending_employees(user: models.User = Depends(auth.require_role("admin")), db: Session = Depends(get_db)):
    rows = db.query(models.User).filter_by(org_id=user.org_id, pending_approval=True).all()
    return [user_out(u, True) for u in rows]


@app.post("/org/employees/{emp_id}/approve")
def approve_employee(emp_id: int, user: models.User = Depends(auth.require_role("admin")),
                     db: Session = Depends(get_db)):
    emp = db.query(models.User).get(emp_id)
    if not emp or emp.org_id != user.org_id:
        raise HTTPException(404, "No such employee")
    emp.pending_approval = False
    db.commit()
    log(db, user.org_id, user.id, "employee_approved", emp.name)
    return {"ok": True}


# ------------------------------------------------------- cross-department access -----

@app.post("/access-requests")
def request_access(req: schemas.AccessRequestCreate, user: models.User = Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    org = db.query(models.Organization).get(user.org_id)
    if req.department not in org_departments(org):
        raise HTTPException(400, f"Unknown department '{req.department}'")
    if req.department == user.department or req.department == "All":
        raise HTTPException(400, "You already have access to your own department")
    ar = models.AccessRequest(org_id=user.org_id, requester_id=user.id, department=req.department,
                              reason=req.reason.strip(), duration_hours=req.duration_hours)
    db.add(ar)
    db.commit()
    db.refresh(ar)
    head = dept_head(db, user.org_id, req.department)
    if head:
        notify(db, user.org_id, user.id, head.id, "access_request",
               f"{user.name} requests {req.duration_hours}h access to {req.department}"
               + (f" — {req.reason.strip()}" if req.reason.strip() else ""))
    db.commit()
    log(db, user.org_id, user.id, "access_requested", f"{req.department}, {req.duration_hours}h")
    return {"id": ar.id, "status": ar.status, "routed_to": head.name if head else None}


@app.get("/access-requests")
def list_access_requests(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    mine = db.query(models.AccessRequest).filter_by(org_id=user.org_id, requester_id=user.id).order_by(
        models.AccessRequest.created_at.desc()).all()
    for_review = []
    if auth.rank(user) >= auth.ROLE_RANK["manager"]:
        depts = {user.department} if user.role == "manager" else set(org_departments(
            db.query(models.Organization).get(user.org_id)))
        for_review = db.query(models.AccessRequest).filter(
            models.AccessRequest.org_id == user.org_id, models.AccessRequest.status == "pending",
            models.AccessRequest.department.in_(depts)).order_by(models.AccessRequest.created_at).all()

    def out(a, names):
        return {"id": a.id, "department": a.department, "reason": a.reason, "status": a.status,
                "duration_hours": a.duration_hours, "requester": names.get(a.requester_id, "?"),
                "created_at": a.created_at.isoformat()}
    names = {u.id: u.name for u in db.query(models.User).filter_by(org_id=user.org_id)}
    return {"mine": [out(a, names) for a in mine], "for_review": [out(a, names) for a in for_review]}


@app.post("/access-requests/{req_id}/decide")
def decide_access_request(req_id: int, req: schemas.AccessRequestDecide,
                          user: models.User = Depends(auth.require_role("manager")),
                          db: Session = Depends(get_db)):
    ar = db.query(models.AccessRequest).get(req_id)
    if not ar or ar.org_id != user.org_id:
        raise HTTPException(404, "No such request")
    if ar.status != "pending":
        raise HTTPException(400, "Already decided")
    if user.role == "manager" and user.department != ar.department:
        raise HTTPException(403, "Only that department's manager (or an admin/owner) can decide this")
    ar.status = "approved" if req.approve else "denied"
    ar.decided_by = user.id
    ar.decided_at = datetime.datetime.utcnow()
    if req.approve:
        hours = req.duration_hours or ar.duration_hours
        db.add(models.DepartmentGrant(org_id=user.org_id, user_id=ar.requester_id, department=ar.department,
                                      granted_by=user.id,
                                      expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=hours)))
    db.commit()
    notify(db, user.org_id, user.id, ar.requester_id, "access_decision",
           f"Your request for {ar.department} was {ar.status}" + (f" for {req.duration_hours or ar.duration_hours}h" if req.approve else ""))
    db.commit()
    log(db, user.org_id, user.id, "access_" + ar.status, f"{ar.department} for user #{ar.requester_id}")
    return {"ok": True, "status": ar.status}


@app.post("/access-requests/grant")
def direct_grant(req: schemas.DirectGrant, user: models.User = Depends(auth.require_role("manager")),
                 db: Session = Depends(get_db)):
    """A department head sharing access directly, without waiting for a request -
    e.g. handing a specific person a general document from your team."""
    target = db.query(models.User).get(req.user_id)
    if not target or target.org_id != user.org_id:
        raise HTTPException(404, "No such person")
    if user.role == "manager" and user.department != req.department:
        raise HTTPException(403, "You can only share access to your own department")
    org = db.query(models.Organization).get(user.org_id)
    if req.department not in org_departments(org):
        raise HTTPException(400, f"Unknown department '{req.department}'")
    db.add(models.DepartmentGrant(org_id=user.org_id, user_id=target.id, department=req.department,
                                  granted_by=user.id,
                                  expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=req.duration_hours)))
    db.commit()
    notify(db, user.org_id, user.id, target.id, "access_decision",
           f"{user.name} shared {req.department} access with you for {req.duration_hours}h")
    db.commit()
    log(db, user.org_id, user.id, "access_granted", f"{req.department} to user #{target.id}, {req.duration_hours}h")
    return {"ok": True}


@app.patch("/org/employees/{emp_id}")
def update_employee(emp_id: int, req: schemas.EmployeeUpdate,
                    user: models.User = Depends(auth.require_role("admin")), db: Session = Depends(get_db)):
    emp = db.query(models.User).get(emp_id)
    if not emp or emp.org_id != user.org_id:
        raise HTTPException(404, "Employee not found")
    if auth.rank(emp) >= auth.rank(user) and emp.id != user.id:
        raise HTTPException(403, "You can't modify someone at or above your role")
    if req.role is not None:
        if req.role not in ROLES or auth.ROLE_RANK[req.role] >= auth.rank(user):
            raise HTTPException(400, "Invalid role for you to assign")
        emp.role = req.role
    cleanup_reason = None
    if req.department is not None:
        if req.department != emp.department:
            cleanup_reason = f"moved from {emp.department} to {req.department}"
        emp.department = req.department
    if req.supervisor_id is not None:
        _check_supervisor(db, user.org_id, req.supervisor_id)
        emp.supervisor_id = req.supervisor_id or None
    if req.active is not None and emp.id != user.id:
        if emp.active and not req.active:
            cleanup_reason = "deactivated"
        emp.active = req.active
    if cleanup_reason:      # temporary grants / extra access never follow a person to a new role
        features.revoke_extra_access(db, emp, actor_id=user.id, reason=cleanup_reason)
    db.commit()
    log(db, user.org_id, user.id, "employee_updated", f"{emp.name}: {req.model_dump(exclude_none=True)}")
    return {"ok": True}


@app.post("/org/employees/{emp_id}/reset-password")
def reset_password(emp_id: int, user: models.User = Depends(auth.require_role("manager")),
                   db: Session = Depends(get_db)):
    emp = db.query(models.User).get(emp_id)
    if not emp or emp.org_id != user.org_id or auth.rank(emp) >= auth.rank(user):
        raise HTTPException(404, "Employee not found or not allowed")
    pwd = temp_password()
    emp.password_hash = auth.hash_password(pwd)
    emp.must_change_password = True
    db.commit()
    log(db, user.org_id, user.id, "password_reset", emp.name)
    return {"username": emp.username, "temp_password": pwd}


# ----------------------------------------------------------- documents -----

def _index_document(db, user, title, content, visibility, department, allowed, filename=None, ftype="text"):
    if visibility not in VISIBILITIES:
        raise HTTPException(400, "Invalid visibility level")
    if visibility in ("confidential", "restricted") and auth.rank(user) < auth.ROLE_RANK["manager"]:
        raise HTTPException(403, "Only managers and above can upload confidential/restricted documents")
    if auth.rank(user) < auth.ROLE_RANK["manager"]:
        raise HTTPException(403, "Only managers and above can add documents")
    org = db.query(models.Organization).get(user.org_id)
    if department != "All" and department not in org_departments(org):
        raise HTTPException(400, f"Unknown department '{department}'")
    if auth.rank(user) < auth.ROLE_RANK["admin"] and department not in ("All", user.department):
        raise HTTPException(403, "You can only upload to your own department (or All)")
    if department == "All" and visibility != "public" and auth.rank(user) < auth.ROLE_RANK["manager"]:
        department = user.department  # staff uploads default to their own department

    doc = models.Document(org_id=user.org_id, uploaded_by=user.id, owner_id=user.id, title=title, content=content,
                          visibility=visibility, department=department, filename=filename, file_type=ftype,
                          allowed_user_ids=",".join(str(i) for i in (allowed or [])),
                          last_reviewed_at=datetime.datetime.utcnow(), last_reviewed_by=user.id)
    db.add(doc)
    db.flush()
    for i, c in enumerate(retrieval.chunk_text(content)):
        db.add(models.Chunk(document_id=doc.id, org_id=user.org_id, text=c, order_index=i))
    db.commit()
    db.refresh(doc)
    log(db, user.org_id, user.id, "document_upload", f"'{doc.title}' ({doc.visibility}, {doc.department}, {ftype})")
    return {"id": doc.id, "title": doc.title, "visibility": doc.visibility, "department": doc.department}


@app.post("/documents")
def upload_document(req: schemas.DocumentUpload, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    return _index_document(db, user, req.title, req.content, req.visibility, req.department, req.allowed_user_ids)


@app.post("/documents/upload")
async def upload_file(file: UploadFile = File(...), title: str = Form(""), visibility: str = Form("internal"),
                      department: str = Form("All"), allowed_user_ids: str = Form(""),
                      user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (10 MB max)")
    try:
        text = extract.extract_text(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Could not read this file - is it corrupted or password-protected?")
    if len(text.strip()) < 10:
        raise HTTPException(400, "No extractable text found. Scanned/image-only files need OCR, "
                                 "which this prototype doesn't include yet.")
    allowed = [int(x) for x in allowed_user_ids.split(",") if x.strip().isdigit()]
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    return _index_document(db, user, title.strip() or file.filename, text, visibility, department,
                           allowed, file.filename, ext)


@app.get("/documents")
def list_documents(folder_id: Optional[int] = None, user: models.User = Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    docs = db.query(models.Document).filter(models.Document.org_id == user.org_id).order_by(
        models.Document.created_at.desc()).all()
    if folder_id is not None:
        f = db.query(models.Folder).get(folder_id)
        if not f or not _folder_visible(user, f):
            raise HTTPException(404, "Folder not found")
        in_folder = {i.document_id for i in db.query(models.FolderItem).filter_by(folder_id=folder_id)}
        docs = [d for d in docs if d.id in in_folder]
    out = []
    rinfo = insights.review_info(db, user.org_id, [x for x in docs if (x.workspace or "company") == "company"])
    for d in docs:
        # If you can't open it, it isn't listed at all - no locked placeholder,
        # regardless of whether that's a department gate or a visibility-tier gate.
        if not retrieval.user_can_see_document(user, d, db):
            continue
        if (d.workspace or "company") != "company":      # private/pending work lives in My Workspace
            continue
        ri = rinfo.get(d.id, {})
        needs_review = ri.get("needs_review", False)
        body = " ".join(l for l in d.content.split("\n") if l.strip())
        out.append({"id": d.id, "title": d.title, "visibility": d.visibility, "department": d.department,
                    "file_type": d.file_type, "filename": d.filename, "uploaded_by": d.uploaded_by,
                    "owner_id": d.owner_id, "needs_review": needs_review,
                    "review_state": ri.get("review_state"), "review_reasons": ri.get("review_reasons", []),
                    "review_severity": ri.get("review_severity"),
                    "verified_until": d.verified_until.isoformat() if d.verified_until else None,
                    "last_reviewed_at": d.last_reviewed_at.isoformat() if d.last_reviewed_at else None,
                    "created_at": d.created_at.isoformat(), "visible_to_you": True,
                    "preview": body[:220] + ("…" if len(body) > 220 else ""), "length": len(d.content)})
    return out


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """The full document. Same permission gate as search - if you can't open it, it's a 404."""
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    if d.visibility in ("confidential", "restricted"):
        features.record_view(db, user, d)
        log(db, user.org_id, user.id, "document_view", f"'{d.title}' ({d.visibility})")
        db.commit()
    names = {u.id: u.name for u in db.query(models.User).filter_by(org_id=user.org_id)}
    ri = insights.review_info(db, user.org_id, [d])[d.id]
    needs_review = ri["needs_review"]
    mine = [f for f in db.query(models.Folder).filter_by(org_id=user.org_id) if _folder_visible(user, f)]
    member_of = {i.folder_id for i in db.query(models.FolderItem).filter_by(document_id=d.id)}
    return {"id": d.id, "title": d.title, "content": d.content, "visibility": d.visibility,
            "department": d.department, "file_type": d.file_type, "filename": d.filename,
            "owner": names.get(d.owner_id), "uploaded_by": names.get(d.uploaded_by),
            "last_reviewed_by": names.get(d.last_reviewed_by), "needs_review": needs_review,
            "review_state": ri["review_state"], "review_reasons": ri["review_reasons"], "review_severity": ri["review_severity"],
            "verified_until": d.verified_until.isoformat() if d.verified_until else None,
            "last_reviewed_at": d.last_reviewed_at.isoformat() if d.last_reviewed_at else None,
            "created_at": d.created_at.isoformat(),
            "folders": [{"id": f.id, "name": f.name, "scope": f.scope, "can_edit": _folder_can_edit(user, f)}
                        for f in mine if f.id in member_of],
            "addable_folders": [{"id": f.id, "name": f.name, "scope": f.scope} for f in mine
                                if f.id not in member_of and _folder_can_edit(user, f)]}


@app.get("/documents/{doc_id}/summary")
def summarize_document(doc_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """No LLM: an extractive summary - the opening sentence plus the most central sentences."""
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    points = retrieval.summarize(d.content)
    return {"summary": " ".join(points), "points": points}


@app.post("/documents/{doc_id}/verify")
def verify_document(doc_id: int, req: schemas.VerifyDocument, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    if (user.id != d.owner_id and auth.rank(user) < auth.ROLE_RANK["manager"]
            and not insights.is_assigned_reviewer(db, user, d)):
        raise HTTPException(403, "Only the document's owner, a manager or the assigned reviewer can mark it reviewed")
    d.last_reviewed_at = datetime.datetime.utcnow()
    d.last_reviewed_by = user.id
    d.verified_until = datetime.datetime.utcnow() + datetime.timedelta(days=req.days)
    insights.on_document_verified(db, d, user)
    db.commit()
    log(db, user.org_id, user.id, "document_verified", f"{d.title} for {req.days}d")
    return {"ok": True, "verified_until": d.verified_until.isoformat()}


@app.post("/documents/{doc_id}/request-update")
def request_doc_update(doc_id: int, req: schemas.RequestUpdate, user: models.User = Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    d = db.query(models.Document).get(doc_id)
    if not d or d.org_id != user.org_id or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")
    owner = db.query(models.User).get(d.owner_id) if d.owner_id else None
    title = f"Update '{d.title}'" + (f" — {req.note.strip()}" if req.note.strip() else "")
    task = models.Task(org_id=user.org_id, title=title, owner=owner.name if owner else None,
                       assignee_id=owner.id if owner else None, created_by=user.id, is_personal=False)
    db.add(task)
    if owner:
        notify(db, user.org_id, user.id, owner.id, "update_requested", title, document_id=d.id)
    db.commit()
    log(db, user.org_id, user.id, "update_requested", d.title)
    return {"ok": True, "routed_to": owner.name if owner else None}


# ----------------------------------------------------------------- ask -----

def _verified_block(db, rec, sim=None):
    reviewer = db.query(models.User).get(rec.reviewer_id) if rec.reviewer_id else None
    return {"qa_id": rec.id, "answer": rec.corrected_answer or rec.answer_text, "kind": rec.status,
            "similarity": round(sim, 2) if sim is not None else None,
            "reviewer": reviewer.name if reviewer else None,
            "reviewed_at": rec.reviewed_at.isoformat() if rec.reviewed_at else None,
            "note": rec.review_note, "needs_rereview": bool(rec.needs_rereview),
            "rereview_reason": rec.rereview_reason}


def _group_passages(passages):
    groups, order = {}, []
    for p in passages:
        g = groups.get(p["document_id"])
        if g is None:
            g = groups[p["document_id"]] = {"document_id": p["document_id"], "title": p["document_title"],
                                            "department": p.get("department"), "age_days": p.get("age_days", 0),
                                            "score": p.get("score", 0), "passages": []}
            order.append(p["document_id"])
        g["passages"].append(p)
    return [groups[i] for i in order]


@app.post("/ask")
def ask(req: schemas.AskRequest, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first")
    context, thread_of = "", None
    if req.follow_up_to:
        prev = db.query(models.QARecord).get(req.follow_up_to)
        if prev and prev.org_id == user.org_id and prev.user_id == user.id:
            thread_of = prev.id
            context = prev.question
    if req.document_id is not None:
        d = db.query(models.Document).get(req.document_id)
        if not d or not retrieval.user_can_see_document(user, d, db):
            raise HTTPException(404, "Document not found")

    res = retrieval.search_grouped(db, user, question, context=context, document_id=req.document_id)
    scoped = req.document_id is not None
    hop = None if scoped else graph.multi_hop_answer(db, user, question)
    found = None if scoped else retrieval.find_verified(db, user, question)

    verified = None
    answer = res["answer"]
    answer_text = answer["text"] if answer else (("Chain: " + hop["path"]) if hop else None)
    if found:
        rec, sim = found
        verified = _verified_block(db, rec, sim)
        answer_text = verified["answer"]

    stored = []
    for p in res["passages"]:
        entry = dict(p)
        entry.pop("full_chunk", None)
        if answer and p["chunk_id"] == answer["chunk_id"]:
            entry["is_answer"] = True
            entry["answer_text"] = answer["text"]
        stored.append(entry)
    if hop:
        stored.append({"kind": "hop", "path": hop["path"], "hops": hop["hops"]})
    qa = models.QARecord(org_id=user.org_id, user_id=user.id, department=user.department,
                         question=question, answer_text=answer_text, sources=json.dumps(stored),
                         served_from_qa_id=verified["qa_id"] if verified else None, thread_of=thread_of)
    db.add(qa)
    db.commit()
    db.refresh(qa)
    log(db, user.org_id, user.id, "query" if answer_text else "query_no_result", question)
    db.commit()
    conf = features.compute_confidence(db, answer, res["documents"], verified)
    extras = insights.ask_extras(db, user, question, answer_text, verified, res) if not scoped else {}
    if not scoped:
        insights.log_question(db, user, qa, answer_text, verified is not None, conf)
    flag = impact.flag_for(db, qa)
    return {**extras, "qa_id": qa.id, "question": question, "status": qa.status, "created_at": qa.created_at.isoformat(),
            "verified": verified, "answer": answer, "documents": res["documents"], "passages": res["passages"],
            "multi_hop": hop, "answered": bool(answer_text), "thread_of": thread_of,
            "confidence": conf, "impact": flag,
            "state": impact.answer_state(db, bool(answer_text), verified, flag, res["documents"], hop, conf)}


def _root_id(by_id, qa):
    hops = 0
    while qa.thread_of and qa.thread_of in by_id and hops < 500:
        qa = by_id[qa.thread_of]
        hops += 1
    return qa.id


def _message_out(db, user, qa, doc_cache):
    """Rebuild a stored answer for the chat history - re-checking permissions NOW, so an
    expired grant or a changed role hides passages the user can no longer open."""
    passages, hop = [], None
    for src in json.loads(qa.sources or "[]"):
        if src.get("kind") == "hop":
            hop = {"path": src.get("path"), "hops": src.get("hops", [])}
            continue
        did = src.get("document_id")
        if did is None:
            continue          # rows from before chat history existed carry no document ids
        if did not in doc_cache:
            d = db.query(models.Document).get(did)
            doc_cache[did] = d if d and retrieval.user_can_see_document(user, d, db) else None
        if doc_cache[did]:
            passages.append(src)
    answer = None
    for p in passages:
        if p.get("is_answer"):
            answer = {"text": p.get("answer_text") or p["text"], "document_id": p["document_id"],
                      "document_title": p["document_title"], "department": p.get("department"),
                      "chunk_id": p.get("chunk_id")}
    verified = None
    if qa.served_from_qa_id:
        rec = db.query(models.QARecord).get(qa.served_from_qa_id)
        if rec and rec.org_id == user.org_id:
            verified = _verified_block(db, rec)
    rev = db.query(models.User).get(qa.reviewer_id) if qa.reviewer_id else None
    docs_grouped = _group_passages(passages)
    conf = features.compute_confidence(db, answer, docs_grouped, verified)
    flag = impact.flag_for(db, qa)
    return {"qa_id": qa.id, "question": qa.question, "answer_plain": qa.answer_text, "answer": answer,
            "impact": flag, "state": impact.answer_state(db, bool(qa.answer_text), verified, flag, docs_grouped, hop, conf),
            "confidence": conf,
            "verified": verified, "documents": _group_passages(passages), "passages": passages,
            "multi_hop": hop, "answered": bool(qa.answer_text), "status": qa.status,
            "corrected_answer": qa.corrected_answer, "review_note": qa.review_note,
            "reviewer": rev.name if rev else None, "thread_of": qa.thread_of,
            "created_at": qa.created_at.isoformat()}


@app.get("/chats")
def list_chats(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.QARecord).filter_by(user_id=user.id, org_id=user.org_id).order_by(
        models.QARecord.created_at.asc()).all()
    by_id = {r.id: r for r in rows}
    threads = {}
    for r in rows:
        root = _root_id(by_id, r)
        t = threads.setdefault(root, {"id": root, "title": by_id[root].question, "count": 0,
                                      "last_at": r.created_at, "flagged": False})
        t["count"] += 1
        t["last_at"] = max(t["last_at"], r.created_at)
        t["flagged"] = t["flagged"] or r.status == "flagged"
    out = sorted(threads.values(), key=lambda t: t["last_at"], reverse=True)[:80]
    for t in out:
        t["last_at"] = t["last_at"].isoformat()
    return out


@app.get("/chats/{root_id}")
def get_chat(root_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.QARecord).filter_by(user_id=user.id, org_id=user.org_id).order_by(
        models.QARecord.created_at.asc()).all()
    by_id = {r.id: r for r in rows}
    if root_id not in by_id:
        raise HTTPException(404, "Chat not found")
    thread = [r for r in rows if _root_id(by_id, r) == root_id]
    cache = {}
    return {"id": root_id, "title": by_id[root_id].question,
            "messages": [_message_out(db, user, r, cache) for r in thread]}


@app.get("/qa/mine")
def my_questions(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.QARecord).filter_by(user_id=user.id).order_by(
        models.QARecord.created_at.desc()).limit(25).all()
    out = []
    for r in rows:
        rev = db.query(models.User).get(r.reviewer_id) if r.reviewer_id else None
        out.append({"id": r.id, "question": r.question, "answer": r.answer_text, "status": r.status,
                    "flag_note": r.flag_note, "corrected_answer": r.corrected_answer,
                    "review_note": r.review_note, "reviewer": rev.name if rev else None,
                    "created_at": r.created_at.isoformat()})
    return out


@app.post("/qa/{qa_id}/flag")
def flag_answer(qa_id: int, req: schemas.FlagRequest, user: models.User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    qa = db.query(models.QARecord).get(qa_id)
    if not qa or qa.user_id != user.id:
        raise HTTPException(404, "Question not found")
    if qa.status not in ("unreviewed", "rejected"):
        raise HTTPException(400, f"Already {qa.status}")
    qa.status, qa.flag_note = "flagged", req.note
    insights.on_flag(db, qa)
    sup = supervisor_for(db, user)
    if sup:
        notify(db, user.org_id, user.id, sup.id, "review_request",
               f"{user.name} flagged an answer for review: \"{qa.question}\"", qa_id=qa.id)
    db.commit()
    log(db, user.org_id, user.id, "answer_flagged", qa.question)
    return {"status": qa.status, "routed_to": sup.name if sup else None}


def _can_review(user: models.User, qa: models.QARecord) -> bool:
    return (auth.rank(user) >= auth.ROLE_RANK["manager"] and user.org_id == qa.org_id
            and (qa.user_id != user.id or user.role == "owner")
            and retrieval.dept_ok(user, qa.department))


@app.get("/reviews")
def review_queue(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    rows = db.query(models.QARecord).filter_by(org_id=user.org_id, status="flagged").order_by(
        models.QARecord.created_at.desc()).all()
    out = []
    for r in rows:
        if not _can_review(user, r):
            continue
        asker = db.query(models.User).get(r.user_id)
        out.append({"id": r.id, "question": r.question, "answer": r.answer_text,
                    "sources": json.loads(r.sources or "[]"), "asker": asker.name,
                    "department": r.department, "flag_note": r.flag_note,
                    "created_at": r.created_at.isoformat()})
    return out


@app.post("/reviews/{qa_id}")
def resolve_review(qa_id: int, req: schemas.ReviewRequest,
                   user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    qa = db.query(models.QARecord).get(qa_id)
    if not qa or not _can_review(user, qa) or qa.status != "flagged":
        raise HTTPException(404, "Nothing to review here (or it isn't yours to review)")
    if req.verdict == "approve":
        qa.status = "verified"
    elif req.verdict == "correct":
        if not (req.corrected_answer or "").strip():
            raise HTTPException(400, "Write the correct answer")
        qa.status, qa.corrected_answer = "corrected", req.corrected_answer.strip()
    elif req.verdict == "reject":
        qa.status = "rejected"
    else:
        raise HTTPException(400, "verdict must be approve, correct or reject")
    qa.reviewer_id, qa.review_note, qa.reviewed_at = user.id, req.note, datetime.datetime.utcnow()

    fact_msg = ""
    if req.fact and req.fact.subject and req.fact.relation and req.fact.object:
        _, conflict = graph.add_fact(db, user, req.fact.subject, req.fact.relation, req.fact.object)
        fact_msg = " A corrected fact was added to the graph" + (" and a conflict was flagged." if conflict else ".")

    label = {"approve": "verified", "correct": "corrected", "reject": "marked wrong"}[req.verdict]
    insights.on_review_resolved(db, user, qa, req.verdict)
    notify(db, user.org_id, user.id, qa.user_id, "review_result",
           f"{user.name} {label} your question \"{qa.question}\"."
           + (f" Correct answer: {qa.corrected_answer}" if qa.corrected_answer else "") + fact_msg, qa_id=qa.id)
    db.commit()
    log(db, user.org_id, user.id, "answer_reviewed", f"#{qa.id} {req.verdict}: {qa.question}")
    return {"status": qa.status}


# ------------------------------------------------------ work updates -----

@app.post("/updates")
def send_update(req: schemas.UpdateCreate, user: models.User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    """'I used this for that task' notes - go to a chosen person, else the sender's
    supervisor, plus the document's uploader when a document is referenced."""
    if req.kind not in ("used_for", "progress"):
        raise HTTPException(400, "kind must be used_for or progress")
    recipients = set()
    if req.to_user_id:
        target = db.query(models.User).get(req.to_user_id)
        if not target or target.org_id != user.org_id:
            raise HTTPException(404, "Recipient not found")
        recipients.add(target.id)
    else:
        sup = supervisor_for(db, user)
        if sup:
            recipients.add(sup.id)
    title = ""
    if req.document_id:
        doc = db.query(models.Document).get(req.document_id)
        if doc and doc.org_id == user.org_id and retrieval.user_can_see_document(user, doc, db):
            recipients.add(doc.uploaded_by)
            title = f" (source: {doc.title})"
    recipients.discard(user.id)
    if req.kind == "used_for":
        insights.record_usage(db, user, req.qa_id, req.document_id, req.text)
    for rid in recipients:
        notify(db, user.org_id, user.id, rid, req.kind, req.text.strip() + title,
               document_id=req.document_id, qa_id=req.qa_id)
    db.commit()
    log(db, user.org_id, user.id, "update_sent", req.text[:120])
    return {"sent_to": len(recipients)}


@app.get("/updates")
def inbox(box: str = "in", user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    col = models.WorkUpdate.to_user_id if box == "in" else models.WorkUpdate.from_user_id
    rows = db.query(models.WorkUpdate).filter(col == user.id).order_by(
        models.WorkUpdate.created_at.desc()).limit(50).all()
    names = {u.id: u.name for u in db.query(models.User).filter_by(org_id=user.org_id)}
    return [{"id": r.id, "kind": r.kind, "text": r.text, "read": r.read, "qa_id": r.qa_id,
             "from": names.get(r.from_user_id), "to": names.get(r.to_user_id),
             "created_at": r.created_at.isoformat()} for r in rows]


@app.post("/updates/read-all")
def mark_read(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    db.query(models.WorkUpdate).filter_by(to_user_id=user.id, read=False).update({"read": True})
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------- facts -----

@app.post("/facts")
def create_fact(req: schemas.FactCreate, user: models.User = Depends(auth.require_role("manager")),
                db: Session = Depends(get_db)):
    fact, conflict = graph.add_fact(db, user, req.subject, req.relation, req.object, req.document_id)
    log(db, user.org_id, user.id, "fact_added", f"{req.subject} {req.relation} {req.object}"
        + (" (CONFLICT flagged)" if conflict else ""))
    return {"id": fact.id, "subject": fact.subject, "relation": fact.relation, "object": fact.object,
            "status": fact.status, "conflict_id": conflict.id if conflict else None}


@app.get("/facts")
def list_facts(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return [{"id": f.id, "subject": f.subject, "relation": f.relation, "object": f.object,
             "status": f.status, "confidence": f.confidence} for f in graph.visible_facts(db, user)]


@app.get("/graph")
def get_graph(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return graph.graph_json(db, user)


# ----------------------------------------------------------- conflicts -----

@app.get("/conflicts")
def list_conflicts(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.Conflict).filter(models.Conflict.org_id == user.org_id,
                                            models.Conflict.resolved == False).all()  # noqa: E712
    out = []
    for c in rows:
        old, new = db.query(models.Fact).get(c.old_fact_id), db.query(models.Fact).get(c.new_fact_id)
        proposer = db.query(models.User).get(c.proposed_by) if c.proposed_by else None
        out.append({"conflict_id": c.id,
                    "old": {"id": old.id, "subject": old.subject, "relation": old.relation, "object": old.object},
                    "new": {"id": new.id, "subject": new.subject, "relation": new.relation, "object": new.object},
                    "proposed_keep_id": c.proposed_keep_id, "proposed_by": proposer.name if proposer else None,
                    "proposed_note": c.proposed_note})
    return out


@app.post("/conflicts/{conflict_id}/propose")
def propose_conflict(conflict_id: int, req: schemas.ConflictPropose,
                     user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Below manager rank, you can propose which fact looks right - a manager/admin/
    owner still has to approve it via /resolve before it actually takes effect."""
    c = db.query(models.Conflict).get(conflict_id)
    if not c or c.org_id != user.org_id or c.resolved:
        raise HTTPException(404, "Open conflict not found")
    if req.keep_fact_id not in (c.old_fact_id, c.new_fact_id):
        raise HTTPException(400, "That fact isn't part of this conflict")
    c.proposed_keep_id, c.proposed_by, c.proposed_note = req.keep_fact_id, user.id, req.note
    db.commit()
    head = dept_head(db, user.org_id, user.department)
    if head:
        notify(db, user.org_id, user.id, head.id, "conflict_proposed",
               f"{user.name} proposed a resolution for conflict #{c.id}" + (f" — {req.note}" if req.note else ""))
    db.commit()
    log(db, user.org_id, user.id, "conflict_proposed", f"conflict #{c.id}")
    return {"ok": True, "status": f"Proposed by {user.name} — waiting for approval"}


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, req: schemas.ConflictResolve,
                     user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    c = db.query(models.Conflict).get(conflict_id)
    if not c or c.org_id != user.org_id:
        raise HTTPException(404, "Conflict not found")
    old, new = db.query(models.Fact).get(c.old_fact_id), db.query(models.Fact).get(c.new_fact_id)
    keep, drop = (old, new) if req.keep_fact_id == old.id else (new, old)
    keep.status, drop.status, c.resolved, c.resolution_note = "active", "superseded", True, req.note
    c.resolved_at = datetime.datetime.utcnow()
    insights.on_conflict_resolved(db, user, keep, drop)
    db.commit()
    log(db, user.org_id, user.id, "conflict_resolved", f"kept '{keep.subject} {keep.relation} {keep.object}' (fact #{keep.id})")
    flagged = impact.on_fact_superseded(db, user, drop, keep)    # answers built on the losing fact -> needs review
    return {"resolved": True, "kept_fact_id": keep.id, "answers_flagged": flagged or 0}


# --------------------------------------------------------------- tasks -----

def _task_out(t: models.Task):
    return {"id": t.id, "title": t.title, "owner": t.owner, "assignee_id": t.assignee_id,
            "created_by": t.created_by, "personal": bool(t.is_personal), "deadline": t.deadline,
            "status": t.status, "source_fact_id": t.source_fact_id}


@app.post("/tasks")
def create_task(req: schemas.TaskCreate, user: models.User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    assignee = user
    if req.assignee_id and req.assignee_id != user.id and not req.personal:
        assignee = db.query(models.User).get(req.assignee_id)
        if not assignee or assignee.org_id != user.org_id:
            raise HTTPException(404, "Assignee not found")
        if auth.rank(user) < auth.ROLE_RANK["manager"] or auth.rank(assignee) >= auth.rank(user):
            raise HTTPException(403, "You can only assign tasks to people below your role")
        if user.role == "manager" and assignee.department != user.department:
            raise HTTPException(403, "Managers can assign within their own department")
    t = models.Task(org_id=user.org_id, title=req.title, source_fact_id=req.source_fact_id, owner=assignee.name,
                    assignee_id=assignee.id, created_by=user.id, is_personal=req.personal, deadline=req.deadline)
    db.add(t)
    db.flush()
    if assignee.id != user.id:
        notify(db, user.org_id, user.id, assignee.id, "progress", f"{user.name} assigned you a task: {req.title}")
    db.commit()
    db.refresh(t)
    log(db, user.org_id, user.id, "task_created", req.title + (" (personal)" if req.personal else ""))
    return _task_out(t)


@app.get("/tasks")
def list_tasks(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.Task).filter(models.Task.org_id == user.org_id).all()
    return [_task_out(t) for t in rows if not t.is_personal or t.assignee_id == user.id]


@app.post("/tasks/{task_id}/complete")
def complete_task(task_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.Task).get(task_id)
    if not t or t.org_id != user.org_id or (t.is_personal and t.assignee_id != user.id):
        raise HTTPException(404, "Task not found")
    if user.id not in (t.assignee_id, t.created_by) and auth.rank(user) < auth.ROLE_RANK["admin"]:
        raise HTTPException(403, "Only the assignee, the creator, or an admin can complete this task")
    t.status = "done"
    if t.created_by and t.created_by != user.id:
        notify(db, user.org_id, user.id, t.created_by, "progress", f"{user.name} completed: {t.title}")
    db.commit()
    log(db, user.org_id, user.id, "task_completed", t.title)
    return {"id": t.id, "status": t.status}


@app.get("/my-work")
def my_work(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.Task).filter_by(org_id=user.org_id, assignee_id=user.id).all()
    unread = db.query(models.WorkUpdate).filter_by(to_user_id=user.id, read=False).count()
    delegated = [t for t in db.query(models.Task).filter_by(org_id=user.org_id, created_by=user.id).all()
                 if t.assignee_id != user.id]
    pending = 0
    if auth.rank(user) >= auth.ROLE_RANK["manager"]:
        pending = sum(1 for r in db.query(models.QARecord).filter_by(org_id=user.org_id, status="flagged")
                      if _can_review(user, r))
    return {"tasks": [_task_out(t) for t in rows], "delegated": [_task_out(t) for t in delegated],
            "unread_updates": unread, "pending_reviews": pending}


# ---------------------------------------------------------- audit log -----

@app.get("/audit-log")
def audit_log(user: models.User = Depends(auth.require_role("manager")), db: Session = Depends(get_db)):
    rows = db.query(models.AuditLog).filter(models.AuditLog.org_id == user.org_id).order_by(
        models.AuditLog.created_at.desc()).limit(200).all()
    names = {u.id: u.name for u in db.query(models.User).filter_by(org_id=user.org_id)}
    return [{"id": r.id, "action": r.action, "detail": r.detail, "actor": names.get(r.user_id, "system"),
             "created_at": r.created_at.isoformat()} for r in rows]


# ------------------------------------------------------------- folders -----

def _folder_visible(user, f) -> bool:
    if f.org_id != user.org_id:
        return False
    if f.scope == "personal":
        return f.owner_id == user.id
    if f.scope == "company":
        return True
    return auth.rank(user) >= auth.ROLE_RANK["admin"] or user.department == f.department


def _folder_can_edit(user, f) -> bool:
    if not _folder_visible(user, f):
        return False
    if f.scope == "personal":
        return f.owner_id == user.id
    if auth.rank(user) >= auth.ROLE_RANK["admin"] or f.owner_id == user.id:
        return True
    return f.scope == "department" and user.role == "manager" and user.department == f.department


def _folder_docs(db, user, f):
    ids = [i.document_id for i in db.query(models.FolderItem).filter_by(folder_id=f.id)]
    if not ids:
        return []
    docs = db.query(models.Document).filter(models.Document.id.in_(ids)).all()
    # every viewer only ever sees the documents THEY may open, whatever the folder holds
    return sorted([d for d in docs if retrieval.user_can_see_document(user, d, db)], key=lambda d: d.title.lower())


@app.get("/folders")
def list_folders(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    names = {u.id: u.name for u in db.query(models.User).filter_by(org_id=user.org_id)}
    out = []
    for f in db.query(models.Folder).filter_by(org_id=user.org_id).order_by(models.Folder.name).all():
        if _folder_visible(user, f):
            out.append({"id": f.id, "name": f.name, "scope": f.scope, "department": f.department,
                        "owner": names.get(f.owner_id), "count": len(_folder_docs(db, user, f)),
                        "can_edit": _folder_can_edit(user, f)})
    return out


@app.post("/folders")
def create_folder(req: schemas.FolderCreate, user: models.User = Depends(auth.get_current_user),
                  db: Session = Depends(get_db)):
    name, scope, dept = req.name.strip(), req.scope, req.department
    if scope not in ("personal", "department", "company"):
        raise HTTPException(400, "Invalid folder type")
    if scope != "personal" and auth.rank(user) < auth.ROLE_RANK["manager"]:
        raise HTTPException(403, "Only managers and above can create official folders")
    if scope == "department":
        org = db.query(models.Organization).get(user.org_id)
        dept = dept or user.department
        if dept not in org_departments(org):
            raise HTTPException(400, f"Unknown department '{dept}'")
        if auth.rank(user) < auth.ROLE_RANK["admin"] and dept != user.department:
            raise HTTPException(403, "Managers can only create folders for their own department")
    else:
        dept = None
    f = models.Folder(org_id=user.org_id, name=name, scope=scope, department=dept, owner_id=user.id)
    db.add(f)
    log(db, user.org_id, user.id, "folder_created", f"'{name}' ({scope})")
    db.commit()
    db.refresh(f)
    return {"id": f.id, "name": f.name, "scope": f.scope, "department": f.department}


@app.get("/folders/{folder_id}")
def get_folder(folder_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.Folder).get(folder_id)
    if not f or not _folder_visible(user, f):
        raise HTTPException(404, "Folder not found")
    return {"id": f.id, "name": f.name, "scope": f.scope, "department": f.department,
            "can_edit": _folder_can_edit(user, f),
            "documents": [{"id": d.id, "title": d.title, "department": d.department, "visibility": d.visibility,
                           "file_type": d.file_type} for d in _folder_docs(db, user, f)]}


@app.post("/folders/{folder_id}/items")
def add_folder_item(folder_id: int, req: schemas.FolderItemAdd, user: models.User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    f = db.query(models.Folder).get(folder_id)
    if not f or not _folder_visible(user, f):
        raise HTTPException(404, "Folder not found")
    if not _folder_can_edit(user, f):
        raise HTTPException(403, "You can't change this folder")
    d = db.query(models.Document).get(req.document_id)
    if not d or not retrieval.user_can_see_document(user, d, db):
        raise HTTPException(404, "Document not found")     # can't file what you can't open
    if not db.query(models.FolderItem).filter_by(folder_id=f.id, document_id=d.id).first():
        db.add(models.FolderItem(folder_id=f.id, document_id=d.id, added_by=user.id))
        if f.scope != "personal":
            log(db, user.org_id, user.id, "folder_add", f"'{d.title}' -> '{f.name}'")
        db.commit()
    return {"ok": True}


@app.delete("/folders/{folder_id}/items/{doc_id}")
def remove_folder_item(folder_id: int, doc_id: int, user: models.User = Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    f = db.query(models.Folder).get(folder_id)
    if not f or not _folder_visible(user, f):
        raise HTTPException(404, "Folder not found")
    if not _folder_can_edit(user, f):
        raise HTTPException(403, "You can't change this folder")
    db.query(models.FolderItem).filter_by(folder_id=f.id, document_id=doc_id).delete()
    db.commit()
    return {"ok": True}


@app.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.Folder).get(folder_id)
    if not f or not _folder_visible(user, f):
        raise HTTPException(404, "Folder not found")
    if f.owner_id != user.id and auth.rank(user) < auth.ROLE_RANK["admin"]:
        raise HTTPException(403, "Only the creator or an admin can delete this folder")
    db.query(models.FolderItem).filter_by(folder_id=f.id).delete()
    log(db, user.org_id, user.id, "folder_deleted", f"'{f.name}' ({f.scope})")
    db.delete(f)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------- dashboard -----

@app.get("/dashboard")
def dashboard(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    o = user.org_id
    q = db.query(models.QARecord).filter_by(org_id=o)
    visible_docs = sum(1 for d in db.query(models.Document).filter_by(org_id=o)
                       if (d.workspace or "company") == "company" and retrieval.user_can_see_document(user, d, db))
    return {
        "documents": visible_docs,
        "open_conflicts": db.query(models.Conflict).filter_by(org_id=o, resolved=False).count(),
        "open_tasks": db.query(models.Task).filter_by(org_id=o, status="open", is_personal=False).count(),
        "active_facts": db.query(models.Fact).filter_by(org_id=o, status="active").count(),
        "questions": q.count(),
        "verified_answers": q.filter(models.QARecord.status.in_(["verified", "corrected"])).count(),
        "pending_reviews": q.filter_by(status="flagged").count(),
        "people": db.query(models.User).filter_by(org_id=o, active=True).count(),
    }


# ------------------------------------------------- feature pack 2 routes -----

app.include_router(features.router)
app.include_router(insights.router)
app.include_router(impact.router)       # Knowledge Impact + versions + revalidation
app.include_router(knowledge.router)    # Ask the Organization + related questions + Starting Point
app.include_router(llm.router)          # optional local model (Ollama), closed-book


# ------------------------------------------------------- static files -----

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def root():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
