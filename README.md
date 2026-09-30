# Anamnesis — an organizational brain with memory, permission & provenance

FastAPI + SQLite backend, plain HTML/JS frontend. No paid services, no API keys, runs offline on your
machine (only the graph library loads from a CDN).

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
6. `asha` -> **People -> Add employee** -> generated credentials -> log in as that person -> forced password change.

## Not included (on purpose)
OCR for scanned files, generative LLM answers, real Google OAuth, 2FA, emailing credentials, hash-chained audit log.

## Layout
`backend/app/`: `main.py` routes, `models.py`, `auth.py`, `retrieval.py` (permission-aware TF-IDF), `graph.py`
(facts, conflicts, multi-hop), `extract.py` (PDF/DOCX/PPTX text). `frontend/`: `index.html`, `app.js`, `style.css`.
