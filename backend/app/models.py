import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def now():
    return datetime.datetime.utcnow()


class Organization(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    accent = Column(String, default="#22d3ee")          # UI accent colour (customisation)
    departments = Column(Text, default="General,Engineering,IT,HR,Finance")  # comma separated
    created_at = Column(DateTime, default=now)

    users = relationship("User", back_populates="org")
    documents = relationship("Document", back_populates="org")


class AllowedEmail(Base):
    """Only emails in this table may create an organization (founder allow-list)."""
    __tablename__ = "allowed_emails"
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True)
    used = Column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name = Column(String, nullable=False)
    username = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=True, unique=True)   # optional for employees
    password_hash = Column(String, nullable=False)
    # owner | admin | manager | member | intern | guest
    role = Column(String, nullable=False, default="member")
    department = Column(String, default="General")
    supervisor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    must_change_password = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    pending_approval = Column(Boolean, default=False)  # manager-added employees wait for admin/owner sign-off
    created_at = Column(DateTime, default=now)

    org = relationship("Organization", back_populates="users")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    # public | internal | confidential | restricted
    visibility = Column(String, nullable=False, default="internal")
    allowed_user_ids = Column(String, nullable=True, default="")
    department = Column(String, default="All")           # "All" or a department name
    filename = Column(String, nullable=True)
    file_type = Column(String, default="text")           # text | pdf | docx | pptx ...
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)   # responsible for keeping it current
    verified_until = Column(DateTime, nullable=True)      # after this, shows "Needs review"
    last_reviewed_at = Column(DateTime, nullable=True)
    last_reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=now)

    org = relationship("Organization", back_populates="documents")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    text = Column(Text, nullable=False)
    order_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    document = relationship("Document", back_populates="chunks")


class Fact(Base):
    __tablename__ = "facts"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    subject = Column(String, nullable=False)
    relation = Column(String, nullable=False)
    object = Column(String, nullable=False)
    confidence = Column(String, default="sourced")
    status = Column(String, default="active")  # active | superseded | conflicting
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=now)


class Conflict(Base):
    __tablename__ = "conflicts"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    old_fact_id = Column(Integer, ForeignKey("facts.id"), nullable=False)
    new_fact_id = Column(Integer, ForeignKey("facts.id"), nullable=False)
    resolved = Column(Boolean, default=False)
    resolution_note = Column(Text, nullable=True)
    # below-manager roles can propose which side is right; a manager/admin/owner
    # still has to approve before it actually resolves
    proposed_keep_id = Column(Integer, ForeignKey("facts.id"), nullable=True)
    proposed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    proposed_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    title = Column(String, nullable=False)
    source_fact_id = Column(Integer, ForeignKey("facts.id"), nullable=True)
    owner = Column(String, nullable=True)                 # display name of assignee
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_personal = Column(Boolean, default=False)          # private to-do, only the assignee sees it
    deadline = Column(String, nullable=True)
    status = Column(String, default="open")               # open | done
    created_at = Column(DateTime, default=now)


class QARecord(Base):
    """Every question asked + the answer given. This is the feedback-loop table:
    flagged -> reviewed by a supervisor -> verified/corrected answers are served
    first on future matching questions."""
    __tablename__ = "qa_records"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department = Column(String, default="General")
    question = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=True)
    sources = Column(Text, default="[]")                  # JSON list of {document_title, text}
    served_from_qa_id = Column(Integer, nullable=True)    # set when a verified answer was served
    thread_of = Column(Integer, nullable=True)             # qa_id of the question this follows up on
    # unreviewed | flagged | verified | corrected | rejected
    status = Column(String, default="unreviewed")
    flag_note = Column(Text, nullable=True)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    corrected_answer = Column(Text, nullable=True)
    review_note = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)


class WorkUpdate(Base):
    """Lightweight upward communication: 'I used this for that task', progress
    notes, review requests and review outcomes all land in the recipient's inbox."""
    __tablename__ = "work_updates"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kind = Column(String, default="used_for")   # used_for | progress | review_request | review_result
    text = Column(Text, nullable=False)
    document_id = Column(Integer, nullable=True)
    qa_id = Column(Integer, nullable=True)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now)


class DepartmentGrant(Base):
    """Temporary cross-department access, e.g. an IT employee approved to see Sales
    documents until a given time. Checked alongside a user's own department."""
    __tablename__ = "department_grants"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department = Column(String, nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=now)


class AccessRequest(Base):
    __tablename__ = "access_requests"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department = Column(String, nullable=False)
    reason = Column(Text, default="")
    duration_hours = Column(Integer, default=24)
    status = Column(String, default="pending")  # pending | approved | denied
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)


class Folder(Base):
    """Personal folders are private to their creator (pure convenience - they never change
    who can see a document). Department / company folders are official categories created
    by managers and visible according to the normal department rules."""
    __tablename__ = "folders"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name = Column(String, nullable=False)
    scope = Column(String, nullable=False, default="personal")   # personal | department | company
    department = Column(String, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=now)


class FolderItem(Base):
    __tablename__ = "folder_items"
    id = Column(Integer, primary_key=True)
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    added_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=now)
