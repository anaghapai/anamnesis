"""Small, dependency-free NLP layer: tokenising, light stemming and concept matching.

This is what lets "how many days off do I get" find a document that says "18 days of paid
leave" - the two share no keywords, but both mention the LEAVE concept. Everything here is
deterministic and explainable (no generative model). Add your own company vocabulary in
backend/synonyms.json:   {"leave": ["pto", "time off", "holiday"], "vpn": ["remote access"]}
"""
import json
import os
import re
from functools import lru_cache

STOP = set("""a an the and or but if then else of on in at to for from by with without about as into over
under is are was were be been being am do does did done doing have has had having i me my we our you your he
she it its they them their this that these those there here what which who whom whose when where why how can
could should would will shall may might must not no yes so than too very just also any some all each every
more most much many such own same other another get got gets getting please tell know need want like via per
up out off""".split())
# "off" is a stop-word only when not part of a concept phrase (phrases are matched before stop-words).

# concept id -> words / phrases that mean roughly the same thing in a workplace
BASE_GROUPS = {
    "leave": ["leave", "paid leave", "time off", "days off", "day off", "vacation", "holiday", "holidays",
              "pto", "annual leave", "sick leave", "casual leave", "off days", "absence"],
    "remote": ["remote", "remotely", "work from home", "wfh", "hybrid", "telecommute", "work from anywhere",
               "home office", "in office", "onsite", "on site"],
    "computer": ["laptop", "laptops", "computer", "computers", "pc", "notebook", "workstation", "machine",
                 "device", "devices", "macbook", "hardware", "equipment"],
    "password": ["password", "passwords", "passcode", "credential", "credentials", "login", "log in", "sign in",
                 "signin", "authentication", "mfa", "2fa", "token"],
    "rotate": ["rotate", "rotates", "rotation", "reset", "renew", "renewal", "expire", "expires", "expiry",
               "refresh", "regularly", "periodically", "how often"],
    "vpn": ["vpn", "remote access", "secure tunnel", "tunnel"],
    "salary": ["salary", "salaries", "pay", "compensation", "wage", "wages", "remuneration",
               "ctc", "earn", "earnings", "income", "lakh", "payroll"],
    "deadline": ["deadline", "due date", "due", "cutoff", "cut off", "target date", "timeline", "by when",
                 "schedule", "delivery date", "eta"],
    "migration": ["migration", "migrate", "migrating", "move", "moving", "switch", "switchover", "transfer",
                  "cutover", "upgrade"],
    "database": ["database", "databases", "db", "postgres", "postgresql", "mysql", "sql", "datastore", "data store"],
    "lead": ["lead", "leads", "leading", "led", "own", "owns", "owner", "owning", "handle", "handles", "handled",
             "responsible", "in charge", "manage", "manages", "managing", "champion", "point person"],
    "hire": ["hire", "hiring", "recruit", "recruiting", "recruitment", "headcount", "onboard", "onboarding",
             "new joiner", "new joiners", "staffing", "openings", "vacancy", "vacancies"],
    "deploy": ["deploy", "deploys", "deployment", "deployments", "release", "releases", "ship", "shipping",
               "rollout", "roll out", "go live", "push to production", "production push"],
    "rollback": ["rollback", "rollbacks", "roll back", "revert", "undo release"],
    "expense": ["expense", "expenses", "reimbursement", "reimburse", "claim", "claims", "receipt", "receipts",
                "spend", "spending", "bill", "bills", "invoice", "invoices"],
    "budget": ["budget", "budgets", "funding", "funds", "allocation", "approval", "sanction", "cost", "costs"],
    "meeting": ["meeting", "meetings", "sync", "standup", "stand up", "retro", "retros", "retrospective",
                "catch up", "catchup", "huddle", "review call"],
    "sprint": ["sprint", "sprints", "iteration", "cycle", "cadence", "scrum", "agile"],
    "policy": ["policy", "policies", "rule", "rules", "guideline", "guidelines", "procedure", "procedures",
               "protocol", "sop"],
    "help": ["helpdesk", "help desk", "support", "service desk", "ticket", "tickets", "portal", "it desk"],
    "contractor": ["contractor", "contractors", "freelancer", "freelancers", "consultant", "consultants",
                   "vendor", "vendors", "external", "third party", "outsourced", "temp"],
    "employee": ["employee", "employees", "staff", "team member", "team members", "colleague", "colleagues",
                 "personnel", "workforce", "people", "worker", "workers"],
    "confidential": ["confidential", "secret", "private", "sensitive", "classified", "restricted", "nda"],
    "security": ["security", "secure", "breach", "incident", "vulnerability", "threat", "phishing", "malware",
                 "encryption", "encrypted", "firewall"],
    "backup": ["backup", "backups", "back up", "restore", "recovery", "disaster recovery", "dr"],
    "outage": ["outage", "downtime", "down time", "unavailable", "incident", "failure", "crash", "not working"],
    "customer": ["customer", "customers", "client", "clients", "buyer"],
    "sales": ["sales", "revenue", "deal", "deals", "pipeline", "quota", "target", "targets", "selling"],
    "training": ["training", "trainings", "course", "courses", "learning", "workshop", "workshops", "upskill",
                 "certification", "certifications", "orientation"],
    "performance": ["performance", "appraisal", "appraisals", "review", "reviews", "evaluation", "rating",
                    "ratings", "kpi", "kpis", "okr", "okrs", "feedback"],
    "notice": ["notice", "notice period", "resign", "resignation", "resigning", "quit", "exit", "offboarding",
               "terminate", "termination", "leaving the company"],
    "insurance": ["insurance", "medical", "health cover", "mediclaim", "healthcare", "benefits", "benefit"],
    "travel": ["travel", "trip", "trips", "flight", "flights", "hotel", "hotels", "per diem", "commute"],
    "work_hours": ["working hours", "work hours", "office hours", "shift", "shifts", "timings", "attendance",
                   "clock in", "overtime"],
    "software": ["software", "application", "applications", "app", "apps", "tool", "tools", "licence", "license",
                 "licenses", "licences", "subscription", "install", "installation"],
    "access": ["access", "permission", "permissions", "authorise", "authorize", "authorization", "entitlement",
               "allowed", "allow", "grant", "privilege", "privileges"],
    "email": ["email", "emails", "e-mail", "mail", "inbox", "outlook", "gmail"],
    "chat": ["slack", "channel", "channels", "chat", "teams", "message", "messages", "announce", "announced"],
    "concern": ["concern", "concerns", "risk", "risks", "worry", "issue", "issues", "problem", "problems",
                "blocker", "blockers", "raised"],
    "project": ["project", "projects", "initiative", "initiatives", "programme", "program", "workstream"],
    "decide": ["decide", "decided", "decision", "decisions", "agree", "agreed", "confirm", "confirmed",
               "approve", "approved", "approval"],
    "delay": ["delay", "delayed", "postpone", "postponed", "pushed", "pushed back", "extend", "extended",
              "extension", "slip", "slipped", "reschedule", "rescheduled"],
    "rehearsal": ["rehearsal", "staging", "dry run", "trial run", "test run", "pilot"],
    "backend": ["backend", "back end", "server side", "api", "services"],
    "design": ["design", "designer", "designers", "ui", "ux", "mockup", "mockups", "figma"],
    "engineer": ["engineer", "engineers", "developer", "developers", "programmer", "programmers", "dev", "devs",
                 "sde", "swe"],
}

_EXTRA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "synonyms.json")


def _load_groups():
    groups = {k: list(v) for k, v in BASE_GROUPS.items()}
    if os.path.exists(_EXTRA):
        try:
            with open(_EXTRA, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    groups.setdefault(k.lower(), []).extend(str(x).lower() for x in v)
                    groups[k.lower()].append(k.lower())
        except Exception:
            pass   # a broken optional file must never break search
    return groups


WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def stem(w: str) -> str:
    """Deliberately light suffix stripping, applied identically to documents and queries."""
    w = w.lower().strip("'-")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) <= 3 or w.isdigit():
        return w
    for suf, rep in (("ations", "at"), ("ation", "at"), ("ities", "ity"), ("ments", ""), ("ment", ""),
                     ("nesses", ""), ("ness", ""), ("ingly", ""), ("ies", "y"), ("sses", "ss")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + rep
            break
    else:
        for suf in ("ing", "ed", "es", "ly", "er", "s"):
            if w.endswith(suf) and not w.endswith("ss") and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":   # running -> run
                    w = w[:-1]
                break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


class Vocab:
    def __init__(self):
        groups = _load_groups()
        self.phrase_to_concept = {}
        self.word_to_concept = {}   # stem -> set of concept ids
        for cid, items in groups.items():
            for it in items:
                it = it.lower().strip()
                if " " in it or "-" in it:
                    self.phrase_to_concept[it] = cid
                else:
                    self.word_to_concept.setdefault(stem(it), set()).add(cid)
        # longest phrases first so "paid leave" beats "leave"
        ordered = sorted(self.phrase_to_concept, key=len, reverse=True)
        self.phrase_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in ordered) + r")\b") if ordered else None


@lru_cache(maxsize=1)
def vocab() -> Vocab:
    return Vocab()


@lru_cache(maxsize=20000)
def analyze(text: str):
    """-> (units, stems, concepts)
    units    : frozenset of matching units (stems + '~concept' tokens) used for scoring
    stems    : content stems only (used for highlighting / keyword matching)
    concepts : frozenset of concept ids found"""
    v = vocab()
    low = text.lower()
    concepts = set()
    consumed = []
    if v.phrase_re:
        for m in v.phrase_re.finditer(low):
            concepts.add(v.phrase_to_concept[m.group(1)])
            consumed.append((m.start(), m.end()))
    stems, units = set(), set()
    for m in WORD_RE.finditer(low):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        w = m.group(0)
        if w in STOP:
            continue
        st = stem(w)
        if len(st) < 2:
            continue
        stems.add(st)
        units.add(st)
        concepts.update(v.word_to_concept.get(st, ()))
    for c in concepts:
        units.add("~" + c)
    return frozenset(units), frozenset(stems), frozenset(concepts)


def expand_words(text: str) -> set:
    """Plain lowercase words + synonyms for the same concepts (used by the knowledge-graph search)."""
    v = vocab()
    _, stems, concepts = analyze(text)
    words = {w for w in WORD_RE.findall(text.lower()) if w not in STOP and len(w) > 2}
    inverse = {}
    for p, c in v.phrase_to_concept.items():
        inverse.setdefault(c, set()).add(p)
    for w, ccs in v.word_to_concept.items():
        if len(w) > 2 and ccs & concepts:
            words.add(w)
    return words


NUM_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "twelve", "twenty",
             "thirty", "sixty", "ninety", "hundred", "half", "twice", "daily", "weekly", "monthly", "quarterly",
             "yearly", "annually"}


def question_intent(q: str) -> str:
    ql = q.lower()
    if re.search(r"\bhow (many|much|often|long|frequent)", ql) or "how big" in ql:
        return "number"
    if re.search(r"\bwhen\b|\bwhat date\b|\bdeadline\b|\bby what\b", ql):
        return "date"
    if re.search(r"\bwho\b|\bwhom\b|\bwhose\b", ql):
        return "person"
    return "other"
