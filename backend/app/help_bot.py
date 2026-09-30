"""Help & Navigation chatbot.

Deliberately isolated: this module imports NOTHING from models, db or retrieval, and the
route that serves it never receives a database session. It physically cannot read a
company document. It only knows a fixed FAQ about how to use Anamnesis.
No LLM - keyword + fuzzy matching over the FAQ below.
"""
import re
import math
import difflib

# (view to open, question keywords, answer)
FAQ = [
    ("documents", "request access ask permission cannot open locked document no access",
     "If you can't open a document, use **Request access**: Documents -> *Request access to a document*, enter the "
     "document number (from the link a colleague shared), a reason and a duration (1, 7 or 30 days). "
     "The document owner or department manager approves or rejects it, and the access expires by itself. "
     "For a whole department use *Cross-department access* on the same page."),
    ("documents", "restricted meaning classification public internal confidential levels",
     "Every document has a classification. **Public** - anyone the department rule allows. **Internal** - any member. "
     "**Confidential** - managers and above. **Restricted** - only people who are named on the document (plus the org owner). "
     "Opening Confidential and Restricted documents is logged."),
    ("conflicts", "where conflicts page contradiction contradict",
     "Open **Conflicts** in the left menu. It lists facts that contradict each other. Interns and members can *propose* "
     "which side is right; a manager, admin or owner must approve before it is marked resolved."),
    ("conflicts", "propose resolve resolution intern approve conflict",
     "On the Conflicts page choose the version you think is right and press *Propose*. It shows "
     "\"Proposed by <you> -> waiting for approval\" until a manager or owner approves it."),
    ("ask", "ask question how to search answer find chat thread follow up",
     "Go to **Ask**, type a question and press Ask. Answers are copied word-for-word from documents you are allowed to open, "
     "with the source and a confidence level. Keep typing in the same chat to ask follow-ups."),
    ("ask", "confidence high medium low indicator",
     "Confidence is worked out with plain rules, not AI: how strongly the passage matches, how fresh the document is, and "
     "whether a document or answer has been verified by a person. Expand *Why this confidence* to see the reasons."),
    ("ask", "flag wrong outdated incorrect answer report",
     "Under an answer press **Flag as wrong / outdated**. Your supervisor (or the department manager) reviews it and can "
     "approve, correct or reject it. Corrected answers are shown first next time."),
    ("reviews", "review supervisor correct verify answer queue",
     "Managers and above see flagged answers under **Reviews**. Approve, correct or reject; you can also write the correct "
     "fact into the knowledge graph."),
    ("documents", "upload add document file pdf docx pptx",
     "Managers and above use **Documents -> + Add document** to upload PDF, Word, PowerPoint, text files or pasted text. "
     "Everyone else can keep files privately in **My Workspace** and submit them for approval."),
    ("workspace", "workspace private personal notes checklist folder",
     "**My Workspace** is completely private: your own uploads, notes, checklists and personal folders. Nobody else can see "
     "anything in it until you press *Submit for approval*."),
    ("workspace", "submit approval publish share official company document",
     "In **My Workspace** press **Submit for approval** on a personal document, choose the target department and "
     "classification, and it goes to the manager's approval queue. A manager can approve, reject or ask for changes, "
     "and you get a notification either way."),
    ("approvals", "approval queue manager approve reject changes pending submitted",
     "Managers open **Approvals** to see documents people submitted for their department. You can read the full text, then "
     "Approve, Reject or Request changes with a note."),
    ("documents", "verify verified expiry expire needs review reviewed",
     "The owner presses **Mark reviewed** to verify a document until a date. After that date it shows *Needs review*. "
     "Anyone can press **Request update** to create a task for the owner."),
    ("documents", "owner who owns responsible update request",
     "Each document has an owner who keeps it current. Open the document to see the owner and who last reviewed it and when."),
    ("recycle", "delete deleted restore recycle bin trash undo",
     "Deleting a document moves it to the **Recycle Bin** for 30 days. Open Recycle Bin to restore it. "
     "After 30 days it is deleted permanently."),
    ("documents", "tag tags label",
     "Managers and owners can add simple tags to a document from its page. Tags show on the document list and can be searched."),
    ("documents", "pin favorite favourite star bookmark",
     "Press the star on a document to pin it as a favorite. Favorites are only yours and show at the top of the list."),
    ("documents", "comment comments discuss note on document",
     "Open a document and use the Comments box at the bottom. Everyone who can open the document can read the comments."),
    ("documents", "who has access see access list permissions people",
     "Open a document and choose **Who has access**. Owners and managers see everyone who can currently open it and why."),
    ("documents", "just in time link token one time temporary sensitive share",
     "For very sensitive documents the owner can create a **one-time access link** valid for 15 to 60 minutes. "
     "It works once, for one person, and then expires."),
    ("approvals", "dual control two people second approval approve permanent access critical restricted",
     "Changing the classification of a Restricted document, or giving someone permanent access to it, needs two people: "
     "the person who asks and a different manager, admin or owner who approves under **Approvals**."),
    ("health", "health score knowledge quality percent",
     "**Knowledge Health** shows a 0-100 score from simple maths: how many documents were reviewed in 90 days, open conflicts, "
     "open flags, age of verified answers and documents missing an owner or classification. No AI is used, and every point is explained."),
    ("graph", "knowledge graph facts relation",
     "**Knowledge Graph** shows facts as connected dots. Managers can add facts; a fact that contradicts another becomes a conflict."),
    ("people", "add employee create user password temporary login",
     "Managers and above use **People -> + Add employee**. A username and a one-time temporary password are generated and "
     "shown once; the employee must choose a new password on first login."),
    ("account", "change password forgot reset",
     "Change your own password in **My Account**. If you forgot it, ask a manager or admin to reset it from People; you'll get a new "
     "temporary password."),
    ("audit", "audit log history who did",
     "Managers and above can read the **Audit Log**: logins, questions, uploads, reviews, deletions and changes."),
    ("mywork", "tasks todo inbox notifications updates my work",
     "**My Work** holds your private to-dos, tasks assigned to you and your inbox of updates and review results."),
    ("documents", "department isolation see other department invisible why cannot see",
     "You only see documents from your own department plus company-wide ones. Other departments are invisible by design. "
     "Ask that department's manager for temporary access."),
]

STOP = set("a an the is are do does did i me my to of in on for how what where can cannot cant it this that and or "
           "with be you your when which who why should would please tell about need want get".split())


def _tok(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        out.add(w)
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
        if w.endswith("ing") and len(w) > 5:
            out.add(w[:-3])
        if w.endswith("ed") and len(w) > 4:
            out.add(w[:-2])
    return out


_INDEX = [(view, _tok(kw), ans) for view, kw, ans in FAQ]
_VOCAB = sorted({t for _, toks, _ in _INDEX for t in toks})
_DF = {}
for _v, _toks, _a in _INDEX:
    for _t in _toks:
        _DF[_t] = _DF.get(_t, 0) + 1
_IDF = {t: math.log(1 + len(_INDEX) / n) for t, n in _DF.items()}    # rare words matter more

# things that are clearly company-knowledge questions, not "how do I use the app"
_KNOWLEDGE_HINT = re.compile(r"\b(salary|policy|policies|leave days|vpn|budget|revenue|client|contract|password rotate|"
                             r"how many days|who owns|who handled)\b", re.I)


def answer(question: str):
    q = (question or "").strip()
    if not q:
        return {"answer": "Ask me how to use Anamnesis, e.g. \"How do I request access?\"", "go_to": None, "matched": False}
    qt = _tok(q)
    # typo tolerance: map unknown words to the closest FAQ word
    fixed = set(qt)
    for w in qt:
        if w not in _VOCAB and len(w) > 3:
            m = difflib.get_close_matches(w, _VOCAB, n=1, cutoff=0.8)
            if m:
                fixed.add(m[0])
    best, best_score = None, 0.0
    for view, toks, ans in _INDEX:
        shared = fixed & toks
        if not shared:
            continue
        score = sum(_IDF[t] for t in shared) + 0.01 * len(shared)
        if score > best_score:
            best, best_score = (view, ans), score
    if best and best_score >= 1.5:
        return {"answer": best[1], "go_to": best[0], "matched": True}
    if _KNOWLEDGE_HINT.search(q):
        return {"answer": "I'm only the help assistant - I explain how to use Anamnesis and I can't see any company documents. "
                          "To look something up in your organization's knowledge, use **Ask** (it only shows what you're allowed to open).",
                "go_to": "ask", "matched": False}
    return {"answer": "I'm not sure about that one. I can help with: requesting access, what Restricted means, where the "
                      "Conflicts page is, asking questions, My Workspace and approvals, the Recycle Bin, verification, tags, "
                      "comments and the health score. Try one of those in your own words.",
            "go_to": None, "matched": False}
