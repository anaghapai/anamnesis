from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List


class SignupRequest(BaseModel):
    org_name: str
    name: str
    email: EmailStr
    password: str = Field(min_length=6)


class LoginRequest(BaseModel):
    identifier: str          # email OR username
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class ChangePassword(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class EmployeeCreate(BaseModel):
    name: str
    role: str = "member"
    department: str = "General"
    email: Optional[EmailStr] = None
    supervisor_id: Optional[int] = None


class AccessRequestCreate(BaseModel):
    department: str
    reason: str = ""
    duration_hours: int = Field(default=24, ge=1, le=720)  # 1 hour to 30 days


class AccessRequestDecide(BaseModel):
    approve: bool
    duration_hours: Optional[int] = Field(default=None, ge=1, le=720)  # override on approval


class DirectGrant(BaseModel):
    user_id: int
    department: str
    duration_hours: int = Field(default=24, ge=1, le=720)


class EmployeeUpdate(BaseModel):
    role: Optional[str] = None
    department: Optional[str] = None
    supervisor_id: Optional[int] = None
    active: Optional[bool] = None


class OrgSettings(BaseModel):
    name: Optional[str] = None
    accent: Optional[str] = None
    departments: Optional[List[str]] = None


class ProfileUpdate(BaseModel):
    name: str


class DocumentUpload(BaseModel):
    title: str
    content: str
    visibility: str = "internal"
    department: str = "All"
    allowed_user_ids: Optional[List[int]] = None


class FactCreate(BaseModel):
    subject: str
    relation: str
    object: str
    document_id: Optional[int] = None


class AskRequest(BaseModel):
    question: str
    follow_up_to: Optional[int] = None   # qa_id of the earlier question in this chat
    document_id: Optional[int] = None    # optional: only search inside this one document


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scope: str = "personal"              # personal | department | company
    department: Optional[str] = None


class FolderItemAdd(BaseModel):
    document_id: int


class FlagRequest(BaseModel):
    note: Optional[str] = None


class FactIn(BaseModel):
    subject: str
    relation: str
    object: str


class DocEditIn(BaseModel):
    """Optional: also fix the wording in the source document the wrong answer came from."""
    document_id: int
    old_text: str
    new_text: str


class ReviewRequest(BaseModel):
    verdict: str                        # approve | correct | reject
    corrected_answer: Optional[str] = None
    note: Optional[str] = None
    fact: Optional[FactIn] = None       # optionally write the corrected fact to the graph
    doc_edit: Optional[DocEditIn] = None  # optionally correct the source document too (new version)


class UpdateCreate(BaseModel):
    text: str
    kind: str = "used_for"
    to_user_id: Optional[int] = None
    document_id: Optional[int] = None
    qa_id: Optional[int] = None


class ConflictResolve(BaseModel):
    keep_fact_id: int
    note: Optional[str] = None


class ConflictPropose(BaseModel):
    keep_fact_id: int
    note: Optional[str] = None


class VerifyDocument(BaseModel):
    days: int = Field(default=90, ge=1, le=730)


class RequestUpdate(BaseModel):
    note: str = ""


class TaskCreate(BaseModel):
    title: str
    source_fact_id: Optional[int] = None
    assignee_id: Optional[int] = None
    personal: bool = False
    deadline: Optional[str] = None
