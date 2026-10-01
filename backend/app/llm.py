"""Optional local-LLM layer (Ollama) - CLOSED-BOOK by construction.

  * Completely optional: if Ollama isn't running, nothing changes and the extractive answers keep working.
  * The model is a reasoning engine, not a knowledge source. It only ever receives the question plus
    evidence the asker is authorized to open (permissions are re-checked right now), never the database.
  * No evidence -> the model is NOT called at all: "Anamnesis does not have authorized evidence ...".
  * Every sentence the model writes is checked against the evidence (numbers must appear in it, most content
    words must be supported). Unsupported sentences are removed; if nothing survives, Anamnesis abstains.
  * Only LOCAL models are used: Ollama cloud models (names ending in -cloud / :cloud) are refused, so
    nothing leaves the machine.

Setup:  install Ollama, then  `ollama pull qwen2.5:3b`  (or any gemma / qwen / llama model).
Optional env vars:  OLLAMA_HOST (default http://127.0.0.1:11434)   ANAMNESIS_MODEL (force a model name)
"""
import os
import re
import json
import urllib.request
import urllib.error
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from . import models, auth, retrieval, nlp
from .db import get_db

router = APIRouter()
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
PREFER = ("qwen", "gemma", "llama", "phi", "mistral")
ABSTAIN = ("Anamnesis does not have authorized evidence to establish that. "
           "No evidence means no organizational answer.")


INSUFFICIENT = ("Related authorized evidence was found, but it is not enough to answer this reliably. "
                "Review the evidence below or ask the organization.")


def _http(path, payload=None, timeout=5):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_cloud(name: str) -> bool:
    n = name.lower()
    return n.endswith("-cloud") or n.endswith(":cloud") or ":cloud" in n or "-cloud" in n


def status():
    try:
        tags = _http("/api/tags", timeout=1.5)
    except Exception:
        return {"available": False, "model": None, "models": [], "local_only": True,
                "hint": "Ollama is not running. Install it, run `ollama pull qwen2.5:3b`, and reload."}
    names = [m.get("name", "") for m in tags.get("models", []) if m.get("name") and not _is_cloud(m.get("name", ""))]
    forced = os.environ.get("ANAMNESIS_MODEL")
    pick = forced if forced and not _is_cloud(forced) else None
    if not pick:
        for pref in PREFER:
            pick = next((n for n in names if pref in n.lower()), None)
            if pick:
                break
        pick = pick or (names[0] if names else None)
    return {"available": bool(pick), "model": pick, "models": names, "local_only": True,
            "hint": None if pick else "Ollama is running but has no local model. Run `ollama pull qwen2.5:3b`."}


# ----------------------------------------------------------- claim checker ---

_META_WORDS = ("evidence reasoning conflict conflicting support supports supported state states stated say says said "
              "confirm confirms confirmed consistent consistently point points agree agrees disagree disagrees "
              "mention mentions mentioned indicate indicates show shows both different answer question source sources")
META_STEMS = {nlp.stem(w) for w in _META_WORDS.split()}
_NUM = re.compile(r"\d+(?:[.,:/-]\d+)*")
_CITE = re.compile(r"\s*\[E\d+(?:\s*,\s*E\d+)*\]")


def _sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _support(sentence: str, ev_text: str, ev_stems: set) -> float:
    """0..1 - share of the sentence's content words found in the evidence; 0 when a number isn't there."""
    plain = _CITE.sub("", sentence)
    plain = re.sub(r"\bE\d+\b", "", plain)
    for num in _NUM.findall(plain):
        if num not in ev_text:
            return 0.0
    stems = {s for s in nlp.analyze(plain)[1] if len(s) > 2 and s not in META_STEMS}
    if not stems:
        return 1.0
    return len(stems & ev_stems) / len(stems)


def check_claims(answer: str, evidence: List[str]):
    ev_text = " ".join(evidence)
    ev_stems = {s for s in nlp.analyze(ev_text)[1]}
    kept, removed = [], []
    for s in _sentences(answer):
        (kept if _support(s, ev_text, ev_stems) >= 0.6 else removed).append(s)
    return kept, removed


# ---------------------------------------------------------------- evidence ---

def gather_evidence(db: Session, user, qa: models.QARecord):
    """Only what THIS user may open right now (permissions re-checked, like the chat history does)."""
    items, cache = [], {}
    try:
        sources = json.loads(qa.sources or "[]")
    except Exception:
        sources = []
    if qa.served_from_qa_id:
        rec = db.get(models.QARecord, qa.served_from_qa_id)
        if rec is not None and rec.org_id == user.org_id and retrieval.dept_ok(user, rec.department):
            items.append({"label": "Human-verified answer", "text": rec.corrected_answer or rec.answer_text or ""})
    for s in sources:
        if s.get("kind") == "hop":
            if s.get("path"):
                items.append({"label": "Knowledge graph", "text": s["path"]})
            continue
        did = s.get("document_id")
        if did is None:
            continue
        if did not in cache:
            d = db.get(models.Document, did)
            cache[did] = d if d is not None and retrieval.user_can_see_document(user, d, db) else None
        if cache[did] is None:
            continue
        text = " ".join(x for x in (s.get("before"), s.get("match") or s.get("text"), s.get("after")) if x)
        if text.strip():
            items.append({"label": cache[did].title, "text": text})
    return [i for i in items if i["text"].strip()][:6]


SYSTEM = ("You are the reasoning engine inside a private organizational knowledge system. "
          "Answer ONLY from the numbered EVIDENCE. Never use outside knowledge. Never guess. "
          "If the evidence does not establish the answer, reply exactly: INSUFFICIENT_EVIDENCE. "
          "Otherwise write: the direct answer in 1-2 sentences; then a line starting 'Reasoning:' with 2-4 short "
          "sentences explaining which evidence supports the answer and how; then, ONLY if evidence items disagree "
          "(different dates, numbers or owners), a line starting 'Conflict:' naming both sides with their citations. "
          "Cite every sentence like [E1]. Do not add any fact that is not in the evidence.")


@router.get("/llm/status")
def llm_status(user: models.User = Depends(auth.get_current_user)):
    return status()


@router.post("/ask/{qa_id}/explain")
def explain(qa_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    qa = db.get(models.QARecord, qa_id)
    if not qa or qa.user_id != user.id or qa.org_id != user.org_id:
        raise HTTPException(404, "Question not found")
    evidence = gather_evidence(db, user, qa)
    if not evidence:                                            # closed-book: the model is never called
        return {"answer": None, "abstained": True, "status": "no_evidence", "message": ABSTAIN, "evidence": [], "model": None}
    st = status()
    if not st["available"]:
        raise HTTPException(503, st["hint"] or "Local model unavailable")
    block = "\n".join(f"[E{i + 1}] ({e['label']}) {e['text']}" for i, e in enumerate(evidence))
    try:
        res = _http("/api/chat", {"model": st["model"], "stream": False, "think": False,
                                  "options": {"temperature": 0, "num_predict": int(os.environ.get("ANAMNESIS_MAX_TOKENS", "2000"))},
                                  "messages": [{"role": "system", "content": SYSTEM},
                                               {"role": "user", "content": f"EVIDENCE:\n{block}\n\nQUESTION: {qa.question}\n\n/no_think"}]},
                    timeout=300)
        raw = ((res.get("message") or {}).get("content") or "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    except Exception as e:
        raise HTTPException(502, f"Local model call failed: {e}")
    if res.get("done_reason") == "length":
        raise HTTPException(502, "Local model ran out of tokens before answering. Try again.")
    db.add(models.AuditLog(org_id=user.org_id, user_id=user.id, action="ai_explain",
                           detail=f"qa #{qa.id} model={st['model']}"))
    db.commit()
    shown = [{"id": f"E{i + 1}", "label": e["label"], "text": e["text"]} for i, e in enumerate(evidence)]
    if not raw or raw.strip().strip(".").strip().upper() == "INSUFFICIENT_EVIDENCE":
        return {"answer": None, "abstained": True, "status": "insufficient", "message": INSUFFICIENT, "evidence": shown, "model": st["model"]}
    kept, removed = check_claims(raw, [e["text"] for e in evidence])
    if not kept:
        return {"answer": None, "abstained": True, "status": "insufficient", "message": INSUFFICIENT, "removed": removed, "evidence": shown,
                "model": st["model"]}
    return {"answer": " ".join(kept), "abstained": False, "removed": removed, "checked": len(kept) + len(removed),
            "evidence": shown, "model": st["model"]}
