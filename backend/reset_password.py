"""Emergency tool.
  python reset_password.py                       -> list every user (username, email, role)
  python reset_password.py <username|email> <new-password>   -> set that user's password
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from app.db import SessionLocal
from app import models, auth

db = SessionLocal()
if len(sys.argv) < 3:
    for u in db.query(models.User).order_by(models.User.id):
        print(f"{u.id:>3}  {u.username:<14} {str(u.email):<28} {u.role:<8} org={u.org_id} active={u.active}")
    sys.exit(0)
ident, pw = sys.argv[1].strip().lower(), sys.argv[2]
u = db.query(models.User).filter((models.User.email == ident) | (models.User.username == ident)).first()
if not u:
    sys.exit(f"No user '{ident}'. Run without arguments to list users.")
u.password_hash = auth.hash_password(pw)
u.must_change_password = False
u.active = True
db.commit()
print(f"Password for {u.username} ({u.email}) is now: {pw}")
