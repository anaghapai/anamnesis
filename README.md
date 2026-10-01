# Anamnesis — an organizational brain with memory, permission & provenance

FastAPI + SQLite backend, plain HTML/JS frontend. No paid services, no API keys, runs **fully offline** on your
machine (the graph library is in `frontend/vendor/` and the Schibsted Grotesk font is in `frontend/fonts/`; the only internet use is the one-time `pip install`).

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
(A timed 3 to 5 minute version is in `DEMO_SCRIPT.md`.)
1. `priya` asks "how often does the vpn password rotate" -> old answer marked 120 days old -> **Flag**.
2. `meera` -> **Reviews** -> writes the correction (+ optional fact) -> Submit.
3. `priya` asks again -> verified answer comes first; her inbox in **My Work** shows the result.
4. `kabir` (HR) asks the same -> does not see IT's verified answer or IT documents.
5. `priya` asks "CEO salary" -> denied. Ask "who handled the postgres migration" -> multi-hop chain.
6. `priya` opens **Evidence** under an answer -> connected evidence cards. Click **Search vs Anamnesis** for the side-by-side.
7. `priya` -> **Documents** -> **Ask for review** on the VPN doc -> `meera` sees it in **Review Center** -> assigns a reviewer. `asha` -> Review Center -> **Simulate deadline passing** shows the escalation chain.
8. `asha` resolves the Postgres conflict -> **Insights** shows impact, last known truth and who used the old fact.
9. `asha` -> **Audit Log** -> **Verify integrity** (chain intact, head hash saved) -> **Export CSV**.
10. `asha` -> **People -> Add employee** -> generated credentials -> log in as that person -> forced password change.

## Not included (on purpose)
OCR for scanned files, generative LLM answers, real Google OAuth, 2FA, emailing credentials.

## Layout
`backend/app/`: `main.py` routes, `models.py`, `auth.py`, `retrieval.py` (permission-aware TF-IDF), `graph.py`
(facts, conflicts, multi-hop), `extract.py` (PDF/DOCX/PPTX text), `features.py` (feature pack 2), `insights.py` (insights pack), `help_bot.py` (isolated help FAQ bot), `impact.py`, `knowledge.py`, `llm.py`, `orgsearch.py`, `auditview.py`, `auditchain.py`, `evidence.py`. `frontend/`: `index.html`, `app.js` and one plugin file per feature (`features.js`, `insights.js`, `brain.js`, `search.js`, `audit.js`, `evidence.js`, `polish.js`, `landing-v2.js`) with their CSS, plus `fonts/` and `vendor/`.

## Brain pack (Knowledge Impact + Ask the Organization)

- **Knowledge Impact**: edit a document (document page -> *Edit text*) and Anamnesis first **previews** what the change touches, then on apply
  marks every answer / verified answer built on the OLD wording as *needs review* (nothing is deleted), tells the people who received it,
  and gives managers a revalidation queue (*still valid* or *replace*). Every flag has a **why** chain: source -> what changed -> passage -> answer.
  Superseding a graph fact (resolving a conflict) does the same for answers built on the losing fact. Document **version history + compare** included.
- **Answer state badge** on every answer: current / verified / may be outdated / needs review / sources disagree / no knowledge.
- **Ask the Organization**: when nothing answers a question, suggest only people who are *authorized for that knowledge area* (never guessed),
  send them the question (no documents attached), and let them answer / forward / decline. A second authorized person must verify the answer
  before it becomes organizational knowledge.
- **Starting Point** (onboarding: what to read, who to know, good first questions) and **related questions**.
- **Optional local model** (`app/llm.py`): if Ollama is running (`ollama pull qwen2.5:3b`), *Explain (local model)* rewrites the already-authorized
  evidence; it is closed-book, abstains when evidence is missing, and removes claims the evidence does not support. Nothing leaves the machine.
- Upgrading an existing `anamnesis.db` is automatic on first start (new tables + columns are created).

## Polish pack (new)

- **Theme v2 + landing page**: `frontend/theme-v2.css` and `landing-v2.js` (green theme, public landing page, emoji-free labels).
- **Tamper-evident audit log**: each row stores a SHA-256 hash chained to the previous row (`app/auditchain.py`). **Audit Log -> Verify integrity** reports the first edited or missing row. Rows from before this feature are listed as unprotected. Every successful verify also saves the newest hash and row count to `backend/audit_anchor.json`, outside the database, so **deleting only the newest rows is detected** on the next verify. You can paste a head hash you noted earlier into the box next to the button. Limits: someone with full database access can rebuild the whole chain, and someone who can also edit or delete `audit_anchor.json` can reset the anchor, so keep a copy of the head hash off the server. After deliberately resetting the database, delete `audit_anchor.json` (`seed.py` does it for you).
- **Audit CSV export** of the current filters (formula-injection safe, includes each row's hash).
- **Notification bell**, toasts and a loading bar, mobile menu, keyboard shortcuts (`/`, `g` then `d a s w`, `?`), basic accessibility labels, **My Questions**, local-model status line, and **Share access directly** on People. All in `frontend/polish.js` and `polish.css`.
- **Timezone-correct dates** on Search (same fix as the Audit Log).

## Evidence pack (new)

- **Evidence Map** under every answer (collapsed, one click): the question, the answer and its state badge, then cards joined by lines that
  exist in the data (document -> fact -> answer, review -> answer). Card types: document, fact, verified answer, review or flag, conflict,
  and a count of people who marked "I used this". `GET /ask/{id}/evidence` returns only cards for documents the viewer may open;
  hidden documents never appear as titled cards and restricted ones never appear at all.
- **Search vs Anamnesis**: button in the Ask header and a section on the landing page. The left side counts plain keyword matches in what the
  viewer may open and lists what is missing; the right side shows the one cited answer and a small evidence map. The landing page numbers are labelled "Illustration".
- **Knowledge Graph screen**: green theme, node side panel (permission-checked source), neighbour highlight, search, status and department filters, path finder, zoom and fit.
- Files: `backend/app/evidence.py`, `frontend/evidence.js`, `frontend/evidence.css`.

## Tests
```powershell
cd backend
python -m pytest tests -q -p no:warnings          # expect 106 passed (API, permissions, audit chain and head anchor, evidence)
```
Browser smoke test (real Chromium, own temporary database, never touches yours):
```powershell
pip install pytest playwright
python -m playwright install chromium            # one time
cd ..
python -m pytest frontend_tests -q -p no:warnings # expect 6 passed (landing and offline font, login, bell, audit verify, evidence, graph, mobile menu)
```

## Deployment notes
- Run **one** uvicorn process (`uvicorn app.main:app --port 8000`, no `--workers N`). The audit hash chain lock and `audit_anchor.json` are per process.
- SQLite and `audit_anchor.json` live next to the backend; back both up together. Do not commit `*.db` or `audit_anchor.json`.
- Set the `ANAMNESIS_SECRET` environment variable to a long random value (without it a random key is generated on each start, so everyone is logged out after a restart), serve it behind HTTPS (a reverse proxy), and replace the placeholder addresses in `backend/allowed_emails.txt`.
- This is a prototype: thresholds such as 90 days are settings, search is TF-IDF with rules (not a generative model unless Ollama is enabled), and there is no OCR, real Google sign-in or 2FA.
