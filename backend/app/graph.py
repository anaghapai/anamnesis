import re
from typing import List, Optional
from sqlalchemy.orm import Session

from . import models, nlp


def visible_facts(db: Session, user) -> List[models.Fact]:
    """Facts are org-scoped only (not per-document visibility) - they're the
    distilled, human-approved knowledge graph, so any org member can traverse
    it. Swap in per-fact visibility here if your org needs finer control."""
    return (
        db.query(models.Fact)
        .filter(models.Fact.org_id == user.org_id, models.Fact.status != "superseded")
        .all()
    )


def add_fact(db: Session, user, subject: str, relation: str, obj: str,
             document_id: Optional[int] = None, confidence: str = "sourced"):
    """Insert a fact and run conflict detection: if another ACTIVE fact shares
    the same (subject, relation) but a different object, flag both as
    conflicting instead of silently overwriting - this is the headline
    'doesn't lie by accident' behavior."""
    subject_n, relation_n = subject.strip().lower(), relation.strip().lower()

    existing = (
        db.query(models.Fact)
        .filter(
            models.Fact.org_id == user.org_id,
            models.Fact.status == "active",
        )
        .all()
    )
    conflict_with = None
    for f in existing:
        if f.subject.strip().lower() == subject_n and f.relation.strip().lower() == relation_n:
            if f.object.strip().lower() != obj.strip().lower():
                conflict_with = f
            break

    new_fact = models.Fact(
        org_id=user.org_id,
        document_id=document_id,
        subject=subject,
        relation=relation,
        object=obj,
        confidence=confidence,
        status="conflicting" if conflict_with else "active",
        created_by=user.id,
    )
    db.add(new_fact)
    db.flush()  # get new_fact.id without committing

    conflict_row = None
    if conflict_with:
        conflict_with.status = "conflicting"
        conflict_row = models.Conflict(
            org_id=user.org_id,
            old_fact_id=conflict_with.id,
            new_fact_id=new_fact.id,
        )
        db.add(conflict_row)

    db.commit()
    db.refresh(new_fact)
    return new_fact, conflict_row


def graph_json(db: Session, user):
    facts = visible_facts(db, user)
    nodes = {}
    edges = []
    for f in facts:
        for label in (f.subject, f.object):
            if label not in nodes:
                nodes[label] = {"id": label, "label": label}
        edges.append({
            "from": f.subject,
            "to": f.object,
            "label": f.relation,
            "status": f.status,
            "confidence": f.confidence,
            "fact_id": f.id,
        })
    return {"nodes": list(nodes.values()), "edges": edges}


STOPWORDS = {"who", "what", "when", "where", "did", "was", "is", "the", "a", "an",
             "of", "on", "for", "to", "in", "and", "handled", "handle", "assigned"}


def multi_hop_answer(db: Session, user, question: str, max_hops: int = 3):
    """Very deliberately simple: extract candidate entity keywords from the
    question, find facts that touch a keyword as a seed, then walk forward
    one hop at a time - subject -> object -> (that object's own outgoing
    fact) -> ... - never reusing a fact, so the path is a clean chain rather
    than a flood of every edge reachable from the seed. This is what makes
    'who handled the migration nobody wrote down directly' answerable - by
    chaining two separately-sourced facts instead of requiring one document
    to say it outright. Among all seed facts, the longest resulting chain is
    returned as the answer."""
    facts = visible_facts(db, user)
    if not facts:
        return None

    words = [w.strip(",.?!'\"").lower() for w in question.split()]
    keywords = [w for w in words if w and w not in STOPWORDS and w not in nlp.STOP and len(w) > 2]
    # meaning-aware: "database move" should also find facts about a "Postgres Migration"
    keywords = list(set(keywords) | {w for w in nlp.expand_words(question) if len(w) >= 4 and w not in nlp.STOP})
    if not keywords:
        return None

    def matches(label: str) -> bool:
        label_l = label.lower()
        return any(kw in label_l for kw in keywords)

    adjacency = {}
    for f in facts:
        adjacency.setdefault(f.subject.strip().lower(), []).append(f)

    seed_facts = [f for f in facts if matches(f.subject) or matches(f.object)]
    if not seed_facts:
        return None

    def walk(seed: models.Fact):
        chain = [seed]
        used_ids = {seed.id}
        current = seed.object
        for _ in range(max_hops - 1):
            candidates = [f for f in adjacency.get(current.strip().lower(), [])
                          if f.id not in used_ids]
            if not candidates:
                break
            nxt = candidates[0]
            chain.append(nxt)
            used_ids.add(nxt.id)
            current = nxt.object
        return chain

    best_chain = max((walk(f) for f in seed_facts), key=len)

    path_str = " → ".join(
        [best_chain[0].subject] + [f"{f.relation} → {f.object}" for f in best_chain]
    )
    return {
        "path": path_str,
        "hops": [
            {"subject": f.subject, "relation": f.relation, "object": f.object,
             "confidence": f.confidence, "fact_id": f.id}
            for f in best_chain
        ],
    }
