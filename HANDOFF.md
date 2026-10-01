# Anamnesis: handoff for the next AI assistant

Read this first. It is the current state of the project as of 1 Oct 2026.

## What this is
Anamnesis is an organizational knowledge tool built for the Async'26 hackathon (team HACKHIVE, Sovereign AI track).
FastAPI + SQLite backend, plain HTML/JS frontend, fully offline, no paid services, no API keys.
Answers are extractive and cited, never generated. Permissions are checked before search, not only before display.
README.md has the full feature list and the demo script.

## Run and test (Windows PowerShell)
```powershell
cd anamnesis\backend
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python seed.py                                   # demo org and users; delete anamnesis.db first to reset
uvicorn app.main:app --reload                    # http://localhost:8000
python -m pytest tests -q -p no:warnings         # expect 87 passed
```
Demo logins (password `demo1234`): asha owner, rahul Eng manager, meera IT manager, priya IT intern, kabir HR member.

## Hard design rules (do not break)
1. No generative LLM answers. The optional local model (`app/llm.py`, Ollama) only rewrites already-authorized evidence, is closed-book, and abstains without evidence.
2. Permission filtering happens on the server before text enters search or counts. Hidden documents never leak titles, counts or facet values. Restricted documents never hint that they exist.
3. `app/help_bot.py` imports nothing from the database or retrieval code. Keep it that way.
4. The audit log is append-only and hash-chained (`app/auditchain.py`). Never edit or delete audit rows in application code.
5. Honest claims only. The README and UI state limits (no OCR, no real Google OAuth, no 2FA, hash chain is tamper-evident not tamper-proof).

## Layout
- `backend/app/`: `main.py` (core routes), `models.py`, `auth.py`, `retrieval.py`, `graph.py`, `extract.py`, `features.py`, `insights.py`, `impact.py`, `knowledge.py`, `llm.py`, `orgsearch.py` (Search), `auditview.py` (Audit Log API), `auditchain.py` (hash chain, verify, CSV export), `help_bot.py`.
- `backend/tests/`: `conftest.py` copies the backend to a temp folder and seeds a fresh DB, so your real `anamnesis.db` is never touched. Files run in name order and `test_zz_demo_story.py` changes data on purpose, so new tests that need the demo history go in files named `test_zzz*` or later.
- `frontend/`: `index.html`, `app.js` (core), `features.js`, `insights.js`, `brain.js`, `search.js`, `audit.js`, `landing-v2.js`, `polish.js`, plus `style.css`, `features.css`, `insights.css`, `brain.css`, `theme-v2.css`, `polish.css`.

## Conventions and gotchas
- New columns: add them to an `_NEW_COLUMNS` / `ensure_schema(engine)` in the module (see `features.py`, `auditchain.py`) and call it in `main.py`. Existing databases upgrade themselves on start.
- Line endings: `main.py`, `orgsearch.py`, `app.js` use CRLF; `models.py` uses LF. Preserve each file's endings when patching.
- Frontend plugin pattern: each feature file is an IIFE that adds its own `<section id="view-X">`, adds a nav button, and registers `window.FEATURE_VIEWS.X = loader`. Hooks `afterBoot`, `decorateDocs`, `decorateViewer`, `decorateAnswer` exist. Do not rewrite `app.js` to add features.
- `polish.js` wraps `window.api` (loading bar, "Saved" toast on non-GET calls, network error toast) and replaces `window.alert` with a toast. Add paths to the `QUIET` regex there to silence the toast for noisy POSTs. It keeps `origApi` for its own calls.
- Dates: timestamps are stored in UTC. Any date filter must send `tz_offset` (`new Date().getTimezoneOffset()`) and the server must convert the viewer's calendar day to UTC (`auditview._day`, `orgsearch._date`).
- `theme-v2.css` loads after all other CSS and uses `!important` on the colour variables. `landing-v2.js` strips emojis and spaced em dashes from interface labels (buttons, nav, headings), never from documents or answers. Do not put emojis in UI labels.
- Audit chain: `auditchain.py` listens to SQLAlchemy `before_flush` and fills `prev_hash` and `row_hash` for every new `AuditLog`, holding a process lock until the transaction ends. Create audit rows through the normal `db.add(models.AuditLog(...))` calls and the chain is handled for you.
- The theme imports Schibsted Grotesk from Google Fonts. Offline it falls back to Segoe UI.

## How the user wants answers
- Direct, action-oriented. Complete files, not partial patches or "edit line 40" instructions.
- She works on Windows PowerShell. Deliver changes as a zip or files plus one PowerShell block that picks each downloaded file by exact byte size (`Where-Object Length -eq N`) so an old download can never be copied by mistake, then a test command and the expected result (for example "87 passed").
- Never pick "the newest file" from Downloads; that caused wrong files twice.
- `Compress-Archive` on Windows PowerShell 5.1 writes backslash paths; `Expand-Archive` handles them fine.
- Test before handing over. Run the real test suite and, for UI work, drive the app in a headless browser.

## Done
Login and roles, departments and visibility tiers, cited Q&A with multi-hop and conflict detection, supervisor feedback loop, recycle bin, access requests, JIT links, dual control, workspace and approvals, knowledge health, help bot, review center and insights, knowledge impact, Ask the Organization, Search with filters, Audit Log with categories and filters, hash-chained audit log with Verify integrity and CSV export, notification bell, toasts, mobile menu, keyboard shortcuts, My Questions, local-model status, Share access directly, theme v2 and landing page, timezone-correct dates on Search and Audit.

## Left (suggested order)
1. Update the Async'26 pitch deck and registration docs: mention the hash-chained audit log, landing page, and 87 tests. Write a 3 to 5 minute demo script (README demo steps are numbered out of order: 7 appears before 6).
2. Record the audit chain head hash somewhere outside the database (printed in the verify result) so deleting only the newest rows becomes detectable.
3. Bundle the font locally so the theme is truly offline.
4. Frontend tests (Playwright smoke test: login, bell, audit verify, mobile menu).
5. Optional: email notifications (decided to skip for now), real Google sign-in (the allow-list in `signup()` is the drop-in point), 2FA, OCR for scanned files.
6. Deployment notes (single-process uvicorn only; the audit chain lock is per process).

## Before pushing to GitHub
`.gitignore` already excludes `venv/`, `*.db`, `.env`, caches and logs. Do not commit `anamnesis.db` or any `.env`. `backend/allowed_emails.txt` contains placeholder addresses; keep real ones out of a public repo.
