"""Populate a demo organization. Run once:  python seed.py

Demo logins (all password demo1234):
  admin@demo.org    Asha   owner    Management
  manager@demo.org  Rahul  manager  Engineering
  itlead@demo.org   Meera  manager  IT          <- supervisor of the intern
  intern@demo.org   Priya  intern   IT
  hr@demo.org       Kabir  member   HR
(you can also log in with the usernames: asha, rahul, meera, priya, kabir)

Demo story - the feedback loop:
  1. priya (IT intern) asks "how often does the vpn password rotate" -> gets the OLD
     answer from a 120-day-old doc (flagged stale) -> clicks 'Flag as wrong'
  2. meera (IT manager) opens Reviews -> writes the correct answer -> Submit
  3. priya asks again -> the verified correction is served first
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(__file__))

from app.db import SessionLocal, engine
from app import models, auth, retrieval, graph

models.Base.metadata.create_all(bind=engine)
db = SessionLocal()

if db.query(models.Organization).filter_by(name="Cipher Labs").first():
    print("Demo org 'Cipher Labs' already present - delete anamnesis.db to reseed.")
    sys.exit(0)

org = models.Organization(name="Cipher Labs", departments="Management,Engineering,IT,HR,Finance")
db.add(org)
db.flush()


def user(name, username, email, role, dept, sup=None):
    u = models.User(org_id=org.id, name=name, username=username, email=email,
                    password_hash=auth.hash_password("demo1234"), role=role, department=dept,
                    supervisor_id=sup.id if sup else None)
    db.add(u)
    db.flush()
    return u


admin = user("Asha", "asha", "admin@demo.org", "owner", "Management")
manager = user("Rahul", "rahul", "manager@demo.org", "manager", "Engineering", admin)
itlead = user("Meera", "meera", "itlead@demo.org", "manager", "IT", admin)
intern = user("Priya", "priya", "intern@demo.org", "intern", "IT", itlead)
hr = user("Kabir", "kabir", "hr@demo.org", "member", "HR", admin)


def add_doc(title, content, visibility, by, dept="All", allowed=None, age_days=0, tags=""):
    reviewed = age_days < 90          # recently created documents count as reviewed; the 120-day-old VPN doc does not
    doc = models.Document(tags=tags, last_reviewed_at=datetime.datetime.utcnow() - datetime.timedelta(days=age_days) if reviewed else None,
                          last_reviewed_by=by.id if reviewed else None, org_id=org.id, uploaded_by=by.id, owner_id=by.id, title=title, content=content,
                          visibility=visibility, department=dept, file_type="text",
                          allowed_user_ids=",".join(str(u) for u in (allowed or [])),
                          created_at=datetime.datetime.utcnow() - datetime.timedelta(days=age_days))
    db.add(doc)
    db.flush()
    for i, c in enumerate(retrieval.chunk_text(content)):
        db.add(models.Chunk(document_id=doc.id, org_id=org.id, text=c, order_index=i))
    return doc


add_doc("Employee Handbook",
        "Cipher Labs operates a hybrid work policy: employees may work remotely up to three days per week. "
        "All expense reports must be submitted within 30 days. The engineering team follows a two-week "
        "sprint cycle with retros every second Friday.", "public", admin, tags="policy,hr")
m1 = add_doc("Sprint Planning Notes - Sept 10",
             "Rahul confirmed he will lead the Postgres migration for Project Alpha. The backend team agreed "
             "the migration deadline is September 25th. Ananya raised a concern about downtime during business hours.",
             "internal", manager)
m2 = add_doc("Backend Sync - Sept 18",
             "Following further review, the Postgres migration deadline was pushed to October 2nd to allow for "
             "a staging rehearsal. Rahul will still own the migration end to end.", "internal", manager)
add_doc("VPN Access Policy (IT)",
        "The VPN password rotates every 90 days and must be reset through the helpdesk portal. "
        "Contractors receive a separate VPN profile from the IT desk.", "internal", itlead, "IT", age_days=120, tags="vpn,security")
add_doc("Laptop Setup Guide (IT)",
        "New laptops are imaged by the IT desk within two working days. Request a laptop through the "
        "helpdesk portal and include the employee's department.", "internal", itlead, "IT", age_days=10)
add_doc("Leave Policy (HR)",
        "Employees receive 18 days of paid leave per year. Unused leave up to 5 days carries over to the "
        "next calendar year.", "internal", hr, "HR")
add_doc("Engineering Runbook",
        "Production deploys happen on Tuesdays and Thursdays. Rollbacks must be announced in the engineering channel.",
        "internal", manager, "Engineering")
add_doc("Q3 Headcount Plan",
        "Engineering plans to hire two backend engineers and one designer in Q4, pending budget approval from finance.",
        "confidential", admin)
add_doc("CEO Compensation Review",
        "The CEO's current annual salary is 42 lakh INR, reviewed quarterly by the board. This figure is "
        "confidential and must not be shared outside the leadership team.",
        "restricted", admin, allowed=[admin.id, manager.id])
db.commit()

graph.add_fact(db, manager, "Rahul", "owns", "Postgres Migration", m1.id)
graph.add_fact(db, manager, "Postgres Migration", "part_of", "Project Alpha", m1.id)
graph.add_fact(db, manager, "Postgres Migration", "deadline", "September 25", m1.id)
_, conflict = graph.add_fact(db, manager, "Postgres Migration", "deadline", "October 2", m2.id)
graph.add_fact(db, admin, "Ananya", "raised_concern_about", "Postgres Migration", m1.id)
graph.add_fact(db, admin, "Project Alpha", "sponsored_by", "Engineering Org", None)

# a personal to-do and an assigned task so My Work isn't empty
db.add(models.Task(org_id=org.id, title="Reset my VPN token", owner="Priya", assignee_id=intern.id,
                   created_by=intern.id, is_personal=True, deadline="Fri"))
db.add(models.Task(org_id=org.id, title="Inventory the IT storeroom laptops", owner="Priya",
                   assignee_id=intern.id, created_by=itlead.id, deadline="Oct 3"))
# a private workspace document + note for the intern (only priya can ever see these)
personal = models.Document(org_id=org.id, uploaded_by=intern.id, owner_id=intern.id, title="Priya - IT onboarding cheat sheet",
                           content="Helpdesk portal handles laptop requests. Ask Meera before resetting anyone's VPN token. "
                                   "Storeroom inventory happens on the first Friday of the month.",
                           visibility="internal", department="IT", file_type="text", workspace="personal")
db.add(personal)
db.flush()
for i, c in enumerate(retrieval.chunk_text(personal.content)):
    db.add(models.Chunk(document_id=personal.id, org_id=org.id, text=c, order_index=i))
db.add(models.Note(org_id=org.id, user_id=intern.id, kind="checklist", title="First-week checklist",
                   body='[{"t":"Get laptop imaged","done":true},{"t":"VPN profile","done":false},{"t":"Meet Meera 1:1","done":false}]'))
db.commit()
# A brand-new database starts a brand-new audit chain, so an old saved head hash no longer applies.
try:
    from app.auditchain import ANCHOR_FILE
    if os.path.exists(ANCHOR_FILE):
        os.remove(ANCHOR_FILE)
except Exception:
    pass
print("Seeded 'Cipher Labs'. See the docstring at the top of seed.py for logins and the demo story.")
