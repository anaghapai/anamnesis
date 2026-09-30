"""Permission-aware, meaning-aware extractive retrieval (no generative model).

Pipeline for every question:
  1. permission gate  - only chunks of documents the asker may open are ever considered
  2. concept matching - words are stemmed and mapped to concepts ("days off" ~ "paid leave")
  3. chunk ranking    - idf-weighted coverage + cosine, with typo tolerance
  4. sentence pick    - the single best sentence(s) become THE answer, not the whole document
  5. grouping         - matching chunks are grouped per document (tabs in the UI) with context
"""
import re
import math
import difflib
import datetime
from typing import List, Optional
from sqlalchemy.orm import Session

from . import models, nlp

# ------------------------------------------------------------------ chunking ---

MAX_CHUNK_CHARS = 420
BULLET_RE = re.compile(r"^\s*([\u2022\u25cf\u25aa\u25e6\u2023\-\*\u2013]|\d{1,2}[.)]|[a-zA-Z][.)])\s+")
ABBR = {"mr.", "mrs.", "ms.", "dr.", "e.g.", "i.e.", "vs.", "etc.", "no.", "inc.", "ltd.", "approx.", "st.",
        "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.", "fig."}
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def split_sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = SENT_SPLIT_RE.split(text)
    out: List[str] = []
    for p in parts:
        if out and out[-1].split(" ")[-1].lower() in ABBR:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    final: List[str] = []
    for s in out:      # a "sentence" with no punctuation at all (pasted blobs) -> windows of ~60 words
        words = s.split(" ")
        if len(s) > 600 and len(words) > 80:
            for i in range(0, len(words), 55):
                final.append(" ".join(words[i:i + 60]))
        else:
            final.append(s)
    return final


def _looks_like_heading(line: str, has_more: bool) -> bool:
    l = line.strip()
    if not l or len(l) > 70 or not has_more or BULLET_RE.match(l):
        return False
    if l.startswith("#"):
        return True
    if l.endswith((".", ",", ";", "?", "!")):
        return False
    words = l.split()
    if len(words) > 9:
        return False
    if l.endswith(":") or l.isupper():
        return True
    caps = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return len(words) >= 1 and caps == len(words) and len(words) <= 6


def split_chunk(text: str):
    """chunk text -> (heading or '', body).  Headings are stored as a first line '## Heading'."""
    if text.startswith("## ") and "\n" in text:
        h, b = text.split("\n", 1)
        return h[3:].strip(), b
    return "", text


def chunk_text(text: str) -> List[str]:
    """Split a document into meaningful chunks: sections (headings), paragraphs, bullets and
    sentences - never more than ~420 characters - so retrieval can point at the exact passage."""
    text = (text or "").replace("\r", "").strip()
    if not text:
        return []
    chunks: List[str] = []
    heading = ""
    buf: List[str] = []       # sentences of the chunk being built
    size = 0

    heading_used = True

    def flush():
        nonlocal buf, size, heading_used
        if buf:
            body = ""
            for item in buf:
                body += ("\n" if item.startswith("\u2022 ") and body else " " if body else "") + item
            chunks.append((f"## {heading}\n" if heading else "") + body.strip())
            heading_used = True
        buf, size = [], 0

    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        para: List[str] = []

        def flush_para(bullet=False):
            nonlocal para, size, buf, heading_used
            if not para:
                return
            for sent in split_sentences(" ".join(para)):
                if bullet:
                    sent = "\u2022 " + sent
                    bullet = False
                if size and size + len(sent) > MAX_CHUNK_CHARS:
                    flush()
                buf.append(sent)
                size += len(sent) + 1
            heading_used = True
            para = []

        for i, line in enumerate(lines):
            if _looks_like_heading(line, has_more=True):
                flush_para(); flush()
                new_h = line.strip().lstrip("#").strip().rstrip(":")
                heading = f"{heading} \u2014 {new_h}" if (heading and not heading_used) else new_h
                heading_used = False
            elif BULLET_RE.match(line):
                flush_para()
                para = [BULLET_RE.sub("", line, count=1).strip()]
                flush_para(bullet=True)
            else:
                para.append(line.strip())
        flush_para()
        if size >= 200:       # paragraph boundary: close reasonably full chunks
            flush()
    flush()
    return chunks or [text[:MAX_CHUNK_CHARS]]


def body_sentences(body: str) -> List[str]:
    """Sentences of a chunk body, keeping bullet lines separate."""
    out: List[str] = []
    for line in body.split("\n"):
        out.extend(split_sentences(line))
    return out


def dept_ok(user: models.User, department: Optional[str], db: Session = None) -> bool:
    """Department gate: company-wide ('All') content is open to everyone in the org;
    department content is visible to that department, admin/owner, and anyone holding
    an unexpired cross-department grant for it."""
    if not department or department == "All":
        return True
    if user.role in ("admin", "owner") or user.department == department:
        return True
    if db is not None:
        hit = db.query(models.DepartmentGrant).filter(
            models.DepartmentGrant.user_id == user.id,
            models.DepartmentGrant.department == department,
            models.DepartmentGrant.expires_at > datetime.datetime.utcnow(),
        ).first()
        if hit:
            return True
    return False


def user_can_see_document(user: models.User, doc: models.Document, db: Session = None) -> bool:
    """The permission gate - runs BEFORE any text reaches the search index for a
    query. Restricted docs are named-user only; everything else must pass BOTH the
    department gate and the visibility-tier gate."""
    if doc.org_id != user.org_id:
        return False
    if doc.visibility == "restricted":
        allowed = {int(x) for x in (doc.allowed_user_ids or "").split(",") if x.strip().isdigit()}
        return user.id in allowed or user.role == "owner"
    if not dept_ok(user, doc.department, db):
        return False
    if doc.visibility == "public" or doc.visibility == "internal":
        return True
    if doc.visibility == "confidential":
        return user.role in ("manager", "admin", "owner")
    return False


def authorized_chunks(db: Session, user: models.User):
    docs = db.query(models.Document).filter(models.Document.org_id == user.org_id).all()
    visible = {d.id: d for d in docs if user_can_see_document(user, d, db)}
    if not visible:
        return [], {}
    chunks = db.query(models.Chunk).filter(models.Chunk.document_id.in_(visible.keys())).all()
    return chunks, visible


# ------------------------------------------------------------------- search ---

MIN_CHUNK_SCORE = 0.32
REL_KEEP = 0.6
_MONTHS = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b\d{1,2}(st|nd|rd|th)\b", re.I)


def _idf_table(chunk_units):
    n = len(chunk_units)
    df = {}
    for units in chunk_units:
        for u in units:
            df[u] = df.get(u, 0) + 1
    return lambda u: math.log(1 + n / (1 + df.get(u, 0))) + 0.2


def _score(q_weights, alts, units, idf):
    """q_weights: {unit: weight}; alts: {unit: set(alternative units, e.g. typo corrections)};
    units: set of units in the text being scored."""
    if not q_weights:
        return 0.0
    tot = sum(q_weights.values())
    hit = 0.0
    cos_num = 0.0
    for u, w in q_weights.items():
        if u in units:
            hit += w
            cos_num += w * idf(u)
        elif alts.get(u) and (alts[u] & units):
            hit += 0.85 * w
            cos_num += 0.85 * w * idf(u)
    coverage = hit / tot if tot else 0.0
    qn = math.sqrt(sum((w * idf(u)) ** 2 for u, w in q_weights.items())) or 1.0
    cn = math.sqrt(sum(idf(u) ** 2 for u in units)) or 1.0
    cos = cos_num / (qn * cn)
    return 0.72 * coverage + 0.28 * min(1.0, cos * 2.5)


def _intent_bonus(intent: str, sentence: str) -> float:
    if intent == "number" and (re.search(r"\d", sentence) or set(re.findall(r"[a-z]+", sentence.lower())) & nlp.NUM_WORDS):
        return 0.10
    if intent == "date" and (_MONTHS.search(sentence) or re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|every|daily|weekly)\b", sentence, re.I)):
        return 0.10
    if intent == "person" and re.search(r"(?<!^)(?<![.!?] )\b[A-Z][a-z]{2,}\b", sentence):
        return 0.06
    return 0.0


FOLLOWUP_RE = re.compile(r"^\s*(and|also|then|so|what about|how about|what if|why|what else|which|is it|does it|do they|"
                         r"can they|can it|how does it|what does it|those|that|it|they|their|its)\b", re.I)


def _is_followup(query: str) -> bool:
    """Short or pronoun-led questions ('what about contractors?') lean on the chat so far;
    a full standalone question ('how do I get a new laptop?') must stand on its own words."""
    _, stems, _ = nlp.analyze(query)
    return len(stems) <= 2 or bool(FOLLOWUP_RE.match(query))


def _build_query(query: str, context: str, vocab_stems):
    q_units, q_stems, _ = nlp.analyze(query)
    own = {u: 1.0 for u in q_units}
    weights = dict(own)
    if context and _is_followup(query):
        c_units, _, _ = nlp.analyze(context)
        for u in c_units:
            weights.setdefault(u, 0.35)      # earlier question of the chat: helps, but never leads
    alts = {}
    for u in list(weights):
        if not u.startswith("~") and len(u) >= 5 and u not in vocab_stems:
            near = difflib.get_close_matches(u, vocab_stems, n=3, cutoff=0.84)
            if near:
                alts[u] = set(near)
    return weights, alts, set(own)


def search_grouped(db: Session, user: models.User, query: str, context: str = "",
                   document_id: Optional[int] = None, max_docs: int = 5):
    """Returns {"answer": {...}|None, "documents": [...], "passages": [...]} - all from
    documents this user is allowed to open."""
    chunks, docs = authorized_chunks(db, user)
    if document_id is not None:
        chunks = [c for c in chunks if c.document_id == document_id]
    if not chunks:
        return {"answer": None, "documents": [], "passages": []}
    chunks.sort(key=lambda c: (c.document_id, c.order_index, c.id))
    analyses = [nlp.analyze(c.text.replace("## ", "", 1)) for c in chunks]
    idf = _idf_table([a[0] for a in analyses])
    vocab_stems = sorted({s for a in analyses for s in a[1]})
    weights, alts, own_units = _build_query(query, context, vocab_stems)
    intent = nlp.question_intent(query)

    scored = []
    for c, a in zip(chunks, analyses):
        # a chunk must match at least one word/concept of the NEW question itself -
        # the chat history alone can never pull in an unrelated document
        own_hit = any(u in a[0] or (alts.get(u) and alts[u] & a[0]) for u in own_units)
        s = _score(weights, alts, a[0], idf) if own_hit else 0.0
        scored.append((s, c))
    top = max((s for s, _ in scored), default=0.0)
    if top < MIN_CHUNK_SCORE:
        return {"answer": None, "documents": [], "passages": []}
    keep = [(s, c) for s, c in scored if s >= max(MIN_CHUNK_SCORE, top * REL_KEEP)]

    by_doc_chunks = {}
    for c in chunks:
        by_doc_chunks.setdefault(c.document_id, []).append(c)

    now = datetime.datetime.utcnow()
    per_doc = {}
    best_sentence = None      # (score, doc, chunk, sentences, index)
    for s, c in sorted(keep, key=lambda p: p[0], reverse=True):
        heading, body = split_chunk(c.text)
        sents = body_sentences(body) or [body]
        sent_scores = []
        for sent in sents:
            su = nlp.analyze(sent)[0] | (nlp.analyze(heading)[0] if heading else frozenset())
            sent_scores.append(0.75 * _score(weights, alts, su, idf) + 0.25 * s + _intent_bonus(intent, sent))
        bi = max(range(len(sents)), key=lambda i: sent_scores[i])
        doc = docs[c.document_id]
        # neighbouring sentences for context (may come from the previous / next chunk)
        before = sents[bi - 1] if bi > 0 else ""
        after = sents[bi + 1] if bi + 1 < len(sents) else ""
        siblings = by_doc_chunks[c.document_id]
        pos = siblings.index(c)
        if not before and pos > 0:
            ps = body_sentences(split_chunk(siblings[pos - 1].text)[1])
            before = ps[-1] if ps else ""
        if not after and pos + 1 < len(siblings):
            ns = body_sentences(split_chunk(siblings[pos + 1].text)[1])
            after = ns[0] if ns else ""
        age_from = max(doc.created_at, doc.last_reviewed_at or doc.created_at)
        passage = {"chunk_id": c.id, "text": sents[bi], "match": sents[bi], "before": before, "after": after,
                   "heading": heading, "full_chunk": body, "score": round(float(s), 3),
                   "document_id": doc.id, "document_title": doc.title, "department": doc.department,
                   "age_days": (now - age_from).days}
        per_doc.setdefault(doc.id, {"document_id": doc.id, "title": doc.title, "department": doc.department,
                                    "visibility": doc.visibility, "age_days": passage["age_days"],
                                    "score": passage["score"], "passages": []})["passages"].append(passage)
        if best_sentence is None or sent_scores[bi] > best_sentence[0]:
            best_sentence = (sent_scores[bi], doc, c, sents, bi, sent_scores)

    groups = sorted(per_doc.values(), key=lambda g: g["score"], reverse=True)[:max_docs]
    for g in groups:
        g["passages"] = g["passages"][:3]

    answer = None
    if best_sentence and best_sentence[0] >= MIN_CHUNK_SCORE * 0.9:
        sc, doc, c, sents, bi, sscores = best_sentence
        text = sents[bi].lstrip("\u2022 ")
        # add the next sentence when it is nearly as relevant (multi-sentence facts)
        if bi + 1 < len(sents) and sscores[bi + 1] >= 0.75 * sc and len(text) + len(sents[bi + 1]) < 420:
            text += " " + sents[bi + 1].lstrip("\u2022 ")
        answer = {"text": text, "document_id": doc.id, "document_title": doc.title,
                  "department": doc.department, "chunk_id": c.id}
    flat = [p for g in groups for p in g["passages"]]
    return {"answer": answer, "documents": groups, "passages": flat}


def search(db: Session, user: models.User, query: str, top_k: int = 3):
    """Backwards-compatible flat list of passages."""
    return search_grouped(db, user, query)["passages"][:top_k]


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def find_verified(db: Session, user: models.User, question: str, threshold: float = 0.45):
    """Human-verified answers (from the supervisor review loop) outrank raw search."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    rows = db.query(models.QARecord).filter(
        models.QARecord.org_id == user.org_id,
        models.QARecord.status.in_(["verified", "corrected"]),
    ).all()
    rows = [r for r in rows if dept_ok(user, r.department)]
    if not rows:
        return None
    try:
        vec = TfidfVectorizer(stop_words="english")
        m = vec.fit_transform([r.question for r in rows] + [question])
        sims = cosine_similarity(m[-1], m[:-1]).flatten()
    except ValueError:
        sims = [0.0] * len(rows)
    qu = nlp.analyze(question)[0]
    best, best_sim = None, 0.0
    for r, s in zip(rows, sims):
        d = _dice(qu, nlp.analyze(r.question)[0])      # meaning-aware match (synonyms count)
        sim = max(float(s), d if d >= 0.6 else 0.0)
        if sim > best_sim:
            best, best_sim = r, sim
    return (best, best_sim) if best is not None and best_sim >= threshold else None


def summarize(content: str, max_sentences: int = 4) -> List[str]:
    """Extractive summary: the opening sentence plus the most 'central' sentences (the ones that
    share the most meaning with the rest of the document), in original order. No generation."""
    sents = []
    for c in chunk_text(content):
        sents.extend(s.lstrip("\u2022 ").strip() for s in body_sentences(split_chunk(c)[1]))
    sents = [s for s in sents if len(s) > 15]
    if len(sents) <= max_sentences:
        return sents
    units = [nlp.analyze(s)[0] for s in sents]
    central = [sum(_dice(u, v) for j, v in enumerate(units) if j != i) for i, u in enumerate(units)]
    picked = {0}
    for i in sorted(range(1, len(sents)), key=lambda i: central[i], reverse=True):
        if len(picked) >= max_sentences:
            break
        picked.add(i)
    return [sents[i] for i in sorted(picked)]
