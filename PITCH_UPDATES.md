# Pitch deck and registration updates (HackHive, Async'26)

The deck file is not in the repo, so this is the text to paste in. Every claim below is true of the current build.

## Add or replace these slides
**Slide: What we built** - Anamnesis, an organizational brain that answers from your own documents, shows where each answer comes
from, and knows what each person is allowed to see. Fully offline: FastAPI + SQLite + plain JavaScript, no API keys, no paid services.

**Slide: Search returns documents. Anamnesis returns understanding.** - Use a screenshot of the Search vs Anamnesis window
(Ask -> Search vs Anamnesis) for "How often does the VPN password rotate?": plain search count on the left, one cited answer with its Evidence Map on the right.

**Slide: Why you can trust an answer** - Evidence Map (document -> fact -> answer, real relationships only), state badge
(current / verified / may be outdated / needs review / sources disagree), confidence line, "why?" chain after a document changes.

**Slide: Sovereign by design** - Permissions are checked on the server before search, so hidden documents never leak titles or counts.
Department isolation, access requests with time limits, dual control. Nothing leaves the machine; an optional local model (Ollama) only rewrites already-authorized evidence.

**Slide: Tamper-evident audit log** - Every action is SHA-256 hash-chained. Verify integrity pinpoints an edited or missing row, and a head hash saved outside the
database catches deletion of the newest rows. CSV export. Honest limit: it is tamper-evident, not tamper-proof.

**Slide: Knowledge that stays alive** - Knowledge Impact (edit a document, preview and flag every answer built on the old wording), conflict detection,
Ask the Organization (routes to authorized experts, second person verifies), supervisor feedback loop, Knowledge Health.

**Slide: Engineering quality** - 106 automated API tests (permissions, audit chain, evidence) plus a real-browser Playwright smoke test.
Landing page, mobile menu, notification bell, keyboard shortcuts, offline font.

**Slide: Honest limits** - Rules and TF-IDF, not a generative model; no OCR for scanned files; no real Google sign-in or 2FA yet;
SQLite, single process; thresholds such as 90 days are prototype settings.

## Registration form text (short)
Anamnesis is a private, offline organizational knowledge system. It answers questions from a company's own documents with citations,
shows an Evidence Map for each answer, enforces permissions before search, detects conflicting facts, flags answers that go stale when documents change,
and keeps a hash-chained audit log. Built by team HackHive for the Sovereign AI track.

## Numbers you can quote
- 106 backend tests passing, 6 browser smoke tests passing
- 0 external network requests when the landing page loads (fonts and graph library are bundled; checked by the browser test)
- 5 demo roles (owner, two managers, intern, member) across 4 departments
