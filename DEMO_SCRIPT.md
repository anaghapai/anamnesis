# Anamnesis: 3-minute demo script (Team HackHive, Async'26, Sovereign AI)

The idea behind the whole talk: **people do not lose knowledge when they lose files. They lose it when nobody can tell which file to trust.**
Everything you say and show serves that one sentence.

Persuasion map: **Pathos** = the first-week intern and the colleague who left. **Ethos** = honest limits, real tests, a working product.
**Logos** = the 120-day-old warning, permissions before search, hash-chained audit log, 106 tests, zero outside requests.

---

## Before you go on stage (10 minutes, do this twice)

1. Reset to a clean demo: stop the server, delete `backend\anamnesis.db` and `backend\audit_anchor.json`, then:
   ```powershell
   cd C:\Users\anagh\Desktop\anamnesis\backend
   python seed.py
   uvicorn app.main:app --port 8000
   ```
2. Open **three separate browser windows** (each keeps its own login, so you never log out on stage):
   - **Window 1, Chrome:** log in as `priya` -> open **Ask**. (password for all demo users: `demo1234`)
   - **Window 2, Chrome Incognito:** log in as `meera`. Leave it on **Dashboard**.
   - **Window 3, Edge:** log in as `kabir` -> open **Ask**.
3. Put the landing page on a 4th tab in Window 1 (open `http://localhost:8000`, log out view) or just keep it ready; you start there.
4. In Window 2 (meera): open **Audit Log** once and press **Verify integrity**. This saves the starting head hash, so on stage it will say how many new rows appeared.
5. Copy this line to your clipboard (it is meera's correction): `The VPN password now rotates every 45 days.`
6. Browser zoom 125 percent, notifications off, full screen, a charged laptop. Turn Wi-Fi off once to prove to yourself it works offline.

---

## 0:00 - 0:30  The hook (30 s)  [pathos, then ethos]

**Show:** the landing page, full screen, nothing else. Do not click. Look at the audience, not the screen, for the first two sentences.

**Say (slowly, pause where you see a dash):**

> "It is your first week at a new job. Nobody has time for you, so you search the company drive.
> Dozens of files. You pick one — you follow it.
> It was out of date by four months. Nobody told you. The one person who knew the right answer left last year,
> and everything she knew walked out the door with her.
> Companies don't lose knowledge when they lose files. They lose it when nobody can tell **which file to trust**.
> We are Team HackHive, and this is **Anamnesis**."

**Delivery:** pause for a full second after "walked out the door with her". Say "which file to trust" slower than everything else.

---

## 0:30 - 1:15  The core: one trustworthy answer (45 s)  [logos]

**Show:** Window 1 (priya), **Ask** screen.

1. Type: `how often does the vpn password rotate` and press Enter.
2. When the answer appears, point (mouse) at the **cited passage**, then at the **"120 days old, may be outdated"** badge.
3. Open **Evidence** under the answer (one click).
4. Click **Search vs Anamnesis** in the Ask header. Let the window sit for 3 seconds. Close it.

**Say:**

> "This is Priya, an IT intern, on her first week. She asks one question.
> Anamnesis does not hand her a pile of files. It gives her **one answer**, with the exact passage it came from —
> and it warns her: this document is a hundred and twenty days old.
> Search would never tell her that.
> Open Evidence: the question, the answer, the source, joined only by relationships that really exist in the data. Nothing invented.
> And this is the difference in one picture: search gives two results with no context, no relationships, no proof.
> **Search returns documents. Anamnesis returns understanding.**"

---

## 1:15 - 2:00  The company learns, and privacy holds (45 s)  [pathos + logos]

**Show:** the three windows, in this order. Practise the switching until it is smooth.

1. Window 1 (priya): click **Flag as wrong / outdated** under the answer. In the box type `Policy changed` and press OK.
2. Window 2 (meera): click **Reviews**. Click into the answer box, paste the correction (Ctrl+V), click **Submit correction**.
3. Window 1 (priya): ask the same question again. The **verified** answer now comes first.
4. Window 3 (kabir, HR): ask the same question. He gets nothing from IT.

**Say:**

> "Priya does not just accept it. She flags it. Her manager Meera gets it, writes the right answer — forty-five days — one click.
> Priya asks again: now the **verified** answer comes first. The company just learned something, **once**, and it will not forget it.
> Now Kabir, from HR, asks the very same question. Nothing. Not hidden, not locked — it does not exist for him.
> Permissions are checked **before** search, not after. Private knowledge stays private."

**If you are running late:** drop step 4 (Kabir) and say only the last two sentences over the verified answer. Never skip the flag-to-verified loop; that is the heart of the product.

---

## 2:00 - 2:30  Under the hood (30 s)  [logos + ethos]

**Show:** Window 2 (meera) -> **Audit Log** -> click **Verify integrity**. Let the green "Chain intact" message with the head hash fill the screen.

**Say:**

> "Under the hood: FastAPI, SQLite and plain JavaScript. No API keys, no cloud, no paid services. It runs fully offline on the company's own machine —
> that is what **sovereign** means to us. Answers are extractive and cited, never generated, so it cannot make things up.
> Every action goes into a hash-chained audit log: change one row and this check tells you exactly which.
> A hundred and six automated tests plus a real-browser test back it up.
> It is a prototype, and we are honest about it: one server today, real Google sign-in is our next step."

---

## 2:30 - 3:00  The close (30 s)  [pathos + call to action]

**Show:** go back to the landing page (or the Evidence panel). Stop clicking. Eyes on the judges for the last line.

**Say:**

> "Every organization has a Priya, in her first week, trusting something that stopped being true.
> And a Meera, who knows the answer and has no time to say it twice.
> Anamnesis is memory that corrects itself: asked once, answered by a human once, remembered by everyone who is allowed to know.
> *Anamnesis* is the Greek word for remembering. We built it so no team ever forgets what it knows.
> We are HackHive. We would love to build this with you. Thank you."

**Delivery:** after "remembering", pause. Final sentence slow and warm. Smile, then stop talking. Do not add "so yeah".

---

## Quick facts to keep in your head (all true of this build)

- 120-day-old document is flagged "may be outdated" (warning starts after 90 days).
- Verified answers come first on similar questions, inside the asker's department.
- Permission check happens on the server before text enters search; hidden documents never leak titles or counts.
- Hash-chained audit log, plus a saved head hash outside the database to catch deletion of the newest rows.
- 106 backend tests, 6 browser tests; the landing page makes zero outside requests (fonts and graph library are bundled).

## Likely judge questions (honest answers)

- **"Is this using GPT or a big model?"** No. Retrieval and rules, extractive and cited. An optional local model through Ollama can rephrase evidence, but it stays off by default, and nothing leaves the machine.
- **"Why would it not hallucinate?"** It never writes new facts. It quotes a passage, shows the source and its age, and abstains when there is nothing.
- **"What stops one department seeing another's data?"** The check runs on the server before search. Cross-department access needs a request, an approval, and expires.
- **"Can someone tamper with the audit log?"** Edited or removed rows are detected. Someone with full control of the server could rebuild everything, so we recommend noting the head hash off-server. It is tamper-evident, not tamper-proof.
- **"Does it scale?"** Prototype on SQLite and one process. The next step is a server database and real sign-in. We say that openly.
- **"No scanned PDFs?"** Text-based PDF, Word, PowerPoint and text files work today; OCR for scans is future work.

## If something breaks on stage

- Answer looks different from the script: the database was not freshly seeded. Reset as in step 1 before the next run.
- A window logged you out: log in again (`demo1234`); do not apologise, keep talking about the idea.
- Audit message says "Chain check failed": delete `backend\audit_anchor.json`, press Verify again.
- Total failure: open `FEATURES.md`, tell the Priya story from the hook, and show one screenshot. The story is the product.

## If you get more than 3 minutes

Add, in this order: (1) ask **who handled the postgres migration** to show the multi-hop chain; (2) as asha, **Knowledge Graph** with the red conflict node, resolve it, then **Insights** to show which answers were affected; (3) **Ask the Organization** with **who approves vpn access**; (4) **Audit Log -> Export CSV**.
