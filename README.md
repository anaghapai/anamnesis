# Anamnesis — an organizational brain with memory, permission & provenance

FastAPI + SQLite backend, plain HTML/JS frontend. No paid services, no API keys, runs **fully offline** on your
machine (the graph library is bundled in `frontend/vendor/`; the only internet use is the one-time `pip install`).

## Run it (Windows PowerShell)
```powershell
cd anamnesis\backend
python -m venv venv
.\venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
python seed.py                      # demo org + users + documents
uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000. To reset all data: stop the server, delete `backend\anamnesis.db`, run `python seed.py`.

## Access model (no open signup)
- **Create an organization**: only emails in `backend/allowed_emails.txt` can do it (one org per email).
  **Edit that file first** with your team's real Gmail addresses, then restart the server.
  Real "Sign in with Google" needs a Google Cloud OAuth Client ID; the allow-list is the offline stand-in.
  The check lives in `signup()` in `app/main.py`, so verifying a Google ID token there is a drop-in upgrade.
- **Employees never self-register.** Owner/admin/manager uses **People -> Add employee**; the system generates a
  username + one-time temporary password (shown once). The employee must change it on first login.
- **Roles**: owner > admin > manager > member > intern > guest. You can only create/assign people below you.
  Managers can only add people to their own department.
- **Departments**: each org has its own list (Org Settings). Documents carry a department (or "All").
  People see "All" + their own department; admin/owner see everything. Each document also has a visibility
  tier: public / internal / confidential (managers+) / restricted (named people only). The permission check
  runs before text enters search, not just before display.

## Features
- Upload **PDF, Word (.docx), PowerPoint (.pptx), TXT/MD/CSV** or paste text (text-based files; scanned images need OCR, not included).
- Cited extractive Q&A, **multi-hop reasoning** over the knowledge graph, **conflict detection**.
- **Stale warning**: passages from documents older than 90 days are marked "may be outdated".
- **Supervisor feedback loop**: flag an answer -> routed to the asker's supervisor (else same-department manager,
  else admin/owner) -> approve / correct / mark wrong, optionally writing a corrected fact to the graph (which
  triggers conflict detection). Approved and corrected answers are served first on similar future questions,
  within the asker's department. This is human-in-the-loop memory correction, not reinforcement learning.
- **"I used this for a task"** notes: one click sends a short note to your supervisor and the document's uploader.
- **My Work**: private to-dos, tasks assigned to you, tasks you delegated, inbox of updates and review results.
- **My Account** (profile, reporting line, change password), **Org Settings** (name, accent colour, departments).
- Append-only **audit log** (managers and above).

## Feature pack 2 (new)

- **Recycle Bin**: delete = soft delete, restorable for 30 days, then purged automatically. **Auto access clean-up** when someone changes department or is deactivated.
- **Access requests per document** (1 / 7 / 30 days, auto-expiring; never confirms a hidden document exists), **who has access** list,
  **just-in-time one-time links** (15-60 min), **view history** for Confidential/Restricted documents, **dual control** (second person) for loosening a Restricted document or giving permanent access.
- **My Workspace**: private uploads, notes, checklists -> **Submit for approval** -> manager **Approvals** queue (approve / reject / request changes). Never searched by Ask, never verified-answer material.
- **Knowledge Health** score (pure arithmetic, every point explained) and a **confidence indicator** (High/Medium/Low from match strength, freshness, verification).
- Document **tags, favorites (pin), comments, related documents** (via the knowledge graph), `?doc=ID` share links.
- **Help & Navigation chatbot** (the ❓ button): `app/help_bot.py` imports nothing from the database or retrieval code, so it cannot read documents.
- Upgrading an existing `anamnesis.db` needs no manual step: missing columns are added automatically on start.

## Insights pack (new)

Everything below is plain rules and arithmetic (no generative model), and every rule is explained in the UI.

- **Criteria-based "Needs review"** (replaces "flag every document with no verified date"). A document is flagged only for a real reason, and the reasons are shown on it:
  high = open conflict / still states a replaced fact / a verified answer built on it needs re-review / someone asked for a review;
  medium = verification expired / answers citing it were flagged / owner missing or deactivated;
  low = never verified and not reviewed for 90+ days. A brand-new upload is not flagged.
- **Ask for review -> owner -> assign -> escalate**: any employee who can open a document can ask for a review. It goes to the owner, who (or a manager) assigns a reviewer ranked by real activity on that topic (only people who can open the document). If nobody acts within 24h (`ANAMNESIS_ESCALATE_HOURS`) it moves up: owner/supervisor -> department manager -> admin/owner. Every hop is written to the audit log. Flagged answers escalate the same way.
- **Review Center** (everyone) and **Insights** hub (managers+).
- **Last Known Truth**: when there is no current answer, show the last verified one, clearly labelled as history.
- **Knowledge-gap learning**: repeated questions with no verified answer are clustered by similarity per department; at 3 asks they are sent to the department manager as a task, and the manager's written answer is served first and the askers are told.
- **Recall notices**: every "I used this" click is stored as an event tied to a specific answer and its facts. When an answer is corrected or a fact is replaced, everyone who used it is notified.
- **Verified-answer invalidation**: replace a document's text and verified answers that cited it go to "needs re-review" (still served, with a warning) instead of silently going stale.
- **Dependency impact explorer** ("affects 12 documents and 3 teams"), **blast-radius queue** (conflicts and gaps ranked by dependent documents x how often asked), **what-if policy preview** (nothing is saved).
- **Expertise decay + knowledge bus factor**: per-topic scores from real uploads, reviews and approvals, halving every 90 days; flags topics that depend on one person (or nobody).
- **Organizational memory diff**: what the organization knew on date A vs date B.
- **Conflict arbitration hint**: suggests which side to trust (author role, verified source, recency). A human still resolves it.
- **Request access instead of a dead end**: if a hidden document would answer your question better, you can send an access request to its owner without ever seeing its title or content. Restricted documents never hint that they exist.
- New tables/columns are added automatically on start; no manual migration.

## Demo logins (password `demo1234`; log in with username or email)
| Username | Email | Role | Dept |
|---|---|---|---|
| asha | admin@demo.org | owner | Management |
| rahul | manager@demo.org | manager | Engineering |
| meera | itlead@demo.org | manager | IT (priya's supervisor) |
| priya | intern@demo.org | intern | IT |
| kabir | hr@demo.org | member | HR |

## Demo script
1. `priya` asks "how often does the vpn password rotate" -> old answer marked 120 days old -> **Flag**.
2. `meera` -> **Reviews** -> writes the correction (+ optional fact) -> Submit.
3. `priya` asks again -> verified answer comes first; her inbox in **My Work** shows the result.
4. `kabir` (HR) asks the same -> does not see IT's verified answer or IT documents.
5. `priya` asks "CEO salary" -> denied. Ask "who handled the postgres migration" -> multi-hop chain.
7. `priya` -> **Documents** -> **Ask for review** on the VPN doc -> `meera` sees it in **Review Center** -> assigns a reviewer. `asha` -> Review Center -> **Simulate deadline passing** shows the escalation chain.
8. `asha` resolves the Postgres conflict -> **Insights** shows impact, last known truth and who used the old fact.
6. `asha` -> **People -> Add employee** -> generated credentials -> log in as that person -> forced password change.

## Not included (on purpose)
OCR for scanned files, generative LLM answers, real Google OAuth, 2FA, emailing credentials, hash-chained audit log.

## Layout
`backend/app/`: `main.py` routes, `models.py`, `auth.py`, `retrieval.py` (permission-aware TF-IDF), `graph.py`
(facts, conflicts, multi-hop), `extract.py` (PDF/DOCX/PPTX text), `features.py` (feature pack 2), `insights.py` (insights pack), `help_bot.py` (isolated help FAQ bot). `frontend/`: `index.html`, `app.js`, `features.js`, `insights.js`, `style.css`, `features.css`, `insights.css`.
