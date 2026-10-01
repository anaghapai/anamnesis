# Anamnesis: every feature in plain English

Anamnesis is an organizational brain. It answers questions from a company's own documents, shows exactly where each answer came from,
keeps private things private, and learns when a human corrects it. Everything runs on one machine, offline, with no API keys.

Anamnesis is the Greek word for remembering.

## The problem it solves
Search finds files. It cannot tell you which file is current, who verified it, or whether you are even allowed to see it.
So people follow outdated documents, ask the same questions again and again, and lose knowledge when someone leaves.

---

## 1. Answers you can trust

| Feature | What it does | Why it matters |
|---|---|---|
| **Cited answers** | Answers a question with the exact passage and the document it came from. Never generated, always extractive. | It cannot invent facts. |
| **Stale warning** | Marks passages from documents older than 90 days as "may be outdated" (the demo document is 120 days old). | People stop trusting old policies by accident. |
| **Confidence indicator** | High / Medium / Low, based on match strength, freshness and whether a human verified it. | You know how much to rely on an answer. |
| **Answer state badge** | current / verified / may be outdated / needs review / sources disagree / no knowledge. | The status is visible at a glance. |
| **Evidence Map** | One click under every answer: the question, the answer, the source document, facts, reviews and conflicts, joined by lines only where a real relationship exists. | Shows *why* the answer deserves trust. |
| **Search vs Anamnesis** | A side-by-side window: plain keyword search (result counts, "no context, no relationships, no proof") versus the one cited answer with its Evidence Map. Also on the landing page. | The 10-second proof of the idea. |
| **Multi-hop reasoning** | Connects facts across documents, for example "who handled the postgres migration", and shows the chain it used. | Answers questions no single document contains. |
| **Conflict detection** | When two sources disagree, both versions are shown side by side and a manager decides. A hint suggests which side to trust (author role, verified source, recency). | Contradictions are surfaced, not buried. |
| **Last Known Truth** | If nothing current answers a question, shows the last verified answer, clearly labelled as history. | Never a dead end. |

## 2. A company that learns

| Feature | What it does | Why it matters |
|---|---|---|
| **Supervisor feedback loop** | Flag a wrong or outdated answer, it goes to your supervisor (else the department manager, else an admin). They approve, correct or reject. Corrected answers are served first next time. | The fix happens once and sticks. (Human-in-the-loop, not reinforcement learning.) |
| **Knowledge Impact** | Edit a document and Anamnesis previews which answers depend on the old wording, then flags them "needs review" (nothing is deleted) and tells the people who received them. A "why?" chain explains each flag. | Changing a policy does not silently leave wrong answers behind. |
| **Ask the Organization** | When nothing answers a question, it suggests only people authorized for that topic, sends them the question, and a second authorized person verifies the answer before it becomes organizational knowledge. | Gets answers from humans without leaking documents. |
| **Knowledge-gap learning** | Questions asked repeatedly with no verified answer are grouped; at 3 asks the department manager gets a task. | The company discovers what it is missing. |
| **Recall notices** | Everyone who clicked "I used this" is told when that answer is corrected. | Wrong information is recalled, like a product. |
| **Review Center, escalation** | Anyone can ask for a document review; it goes to the owner, then a reviewer, then escalates up the chain if nobody acts within 24 hours. | Nothing sits forgotten. |
| **Knowledge Health** | A score for the organization's knowledge, with every point explained. | Managers see where knowledge is weak. |
| **Knowledge bus factor** | Flags topics that depend on one person (or nobody). | Spots the "she left and took it with her" risk early. |
| **Memory diff** | What the organization knew on date A versus date B. | Auditable history of knowledge. |
| **Starting Point** | Onboarding page: what to read, who to know, good first questions. | Faster first week. |

## 3. Privacy and access (the "sovereign" part)

| Feature | What it does | Why it matters |
|---|---|---|
| **Permission checks before search** | The server filters documents before any text enters search or counts. | Hidden documents never leak their title, count or existence. |
| **Departments and visibility tiers** | public / internal / confidential (managers and up) / restricted (named people only). Strict department isolation. | The right people see the right things. |
| **Roles** | owner > admin > manager > member > intern > guest. You can only create people below you. | Clear authority. |
| **Access requests** | Ask for access to a document for 1, 7 or 30 days; it expires on its own. Restricted documents never hint they exist. | Access without permanent exposure. |
| **Just-in-time links, dual control** | One-time links (15 to 60 minutes). A second person must approve loosening a restricted document or giving permanent access. | No single person can quietly open things up. |
| **Admin-created employees** | Nobody self-registers. An admin creates the account, a temporary password is shown once, and the employee must change it at first login. | No open door. |
| **Organization sign-up allow-list** | Only approved emails can create an organization (stand-in for Google sign-in). | Controlled onboarding. |
| **Recycle Bin** | Deleted documents are restorable for 30 days. | Mistakes are reversible. |
| **My Workspace** | Private uploads, notes and checklists. Submitted for manager approval before they become shared knowledge. Never searched by Ask. | Drafts stay private. |

## 4. Trust and accountability

| Feature | What it does | Why it matters |
|---|---|---|
| **Hash-chained audit log** | Every action is recorded, and each row's SHA-256 hash includes the previous row. **Verify integrity** points to the first edited or missing row. | Tamper-evident history. |
| **Head hash anchor** | The newest hash is also saved outside the database, so deleting only the newest rows is caught on the next check. | Closes the "delete the latest rows" gap. |
| **Audit filters and CSV export** | Filter by category, person and date (timezone-correct); export with each row's hash. | Easy review and compliance. |
| **Org-wide Search** | Search documents, answers and facts with filters, always respecting permissions. | Find anything you are allowed to see. |

## 5. Daily comfort

Landing page, green theme with a bundled offline font, notification bell and inbox, toasts, mobile menu, keyboard shortcuts (`/`, `g` then a letter, `?`),
My Work (tasks and delegation), My Questions (history), a help chatbot that can only see help text (never your documents),
Knowledge Graph screen with search, filters, path finder and zoom, and uploads for PDF, Word, PowerPoint, text, Markdown and CSV.

## 6. How it is built
- FastAPI + SQLite + plain JavaScript. No paid services, no API keys, runs fully offline.
- Retrieval is TF-IDF plus rules; answers are extractive. An optional local model (Ollama) can rephrase evidence, closed-book, and it is off by default.
- 106 automated backend tests and 6 browser tests.

## 7. Honest limits (say them out loud, it builds trust)
- Prototype: SQLite and a single server process.
- No OCR for scanned files, no real Google sign-in yet, no 2FA, no email notifications.
- Thresholds such as 90 days are prototype settings.
- The audit log is tamper-evident, not tamper-proof.

## Where this goes next
Real Google sign-in, a server database, OCR for scanned documents, and email or chat notifications.
