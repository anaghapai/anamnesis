# Anamnesis demo script (about 4.5 minutes)

Team HackHive, Async'26, Sovereign AI track. Reset before you start: stop the server, delete `backend\anamnesis.db` and
`backend\audit_anchor.json`, run `python seed.py`, start `uvicorn app.main:app --port 8000`. Use two browser windows
(one normal, one private) so you can switch between people without logging out. Password for every demo user: `demo1234`.

## 0:00 - 0:30  The problem (landing page, no clicking)
"Companies lose knowledge in documents nobody trusts. Search gives you a pile of files. It cannot tell you which one is current,
who verified it, or whether you are even allowed to see it." Scroll to the **Search vs Anamnesis** section on the landing page.
Say: "Search returns documents. Anamnesis returns understanding." Mention it runs fully offline, no API keys, nothing leaves the machine.

## 0:30 - 1:30  Cited answer, stale warning, feedback loop (priya, then meera)
1. Log in as `priya` (IT intern). Ask: **how often does the vpn password rotate**.
2. Point at the answer, the cited passage, the **"120 days old, may be outdated"** badge and the confidence line.
3. Open **Evidence** under the answer: the question, the answer, the source document, joined by real relationships.
   Click **Search vs Anamnesis** to show the plain-search count next to the cited answer for this exact question.
4. Click **Flag as wrong**. Switch to `meera` (IT manager) -> **Reviews** -> type the correction -> Submit.
5. Back as `priya`, ask again: the **verified** answer now comes first and the badge says verified.

## 1:30 - 2:15  Permissions are checked before search (kabir, priya)
1. Log in as `kabir` (HR). Ask the same VPN question: no IT answer, no IT documents, no hint they exist.
2. As `priya` ask **CEO salary**: denied. Say: "Permissions are enforced on the server before text enters search, not just hidden in the UI."

## 2:15 - 3:00  Multi-hop reasoning and conflicts (priya, asha)
1. As `priya` ask **who handled the postgres migration**: show the multi-hop chain in the Evidence Map.
2. Open **Knowledge Graph** as `asha`: the conflict node is red. Resolve the Postgres conflict.
3. Open **Insights** / **Knowledge Impact**: which answers were built on the losing fact, flagged "needs review", with a why chain.

## 3:00 - 3:45  Ask the Organization (priya, meera)
As `priya` ask **who approves vpn access** (nothing in the documents answers it). Click **Ask the organization**:
only people authorized for that area are suggested, never guessed. Answer as `meera`; a second authorized person verifies it before
it becomes organizational knowledge. Asking again returns the verified answer.

## 3:45 - 4:30  Trust and audit (asha)
1. **Audit Log** -> filter by category -> **Verify integrity**: "Chain intact", plus a head hash that is also saved outside the database.
2. Say what it proves and what it does not: each row is hash-chained, so editing or deleting any old row is detected; deleting only the
   newest rows is caught by the saved head hash. Someone with full access to the server could still reset both, so we tell you to note the head hash elsewhere.
3. **Export CSV**.

## Close (4:30 - 4:45)
"Rules and TF-IDF, extractive and cited, not a generative model. 106 API tests and a real-browser smoke test. SQLite prototype, single process."
Honest limits to say out loud: no OCR for scans, no real Google sign-in or 2FA, thresholds like 90 days are prototype settings.

## If something goes wrong
- Blank answer: you are on a database that was not freshly seeded. Reset as above.
- "Chain check failed" after a reset: delete `backend\audit_anchor.json` and verify again.
- Local model status says off: that is fine; answers stay extractive.
