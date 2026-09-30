"""Adds the new columns to an existing anamnesis.db without wiping your data.
Safe to run more than once - it skips columns that are already there.
Run it once, right after copying in the patched files, BEFORE starting the server:

    python migrate.py
"""
import sqlite3, os

DB = os.path.join(os.path.dirname(__file__), "anamnesis.db")
if not os.path.exists(DB):
    raise SystemExit(f"No {DB} found. If you don't have a database yet, just run "
                      "'python seed.py' instead - there's nothing to migrate.")

ADD = {
    "users":     [("pending_approval", "BOOLEAN DEFAULT 0")],
    "documents": [("owner_id", "INTEGER"), ("verified_until", "DATETIME"),
                  ("last_reviewed_at", "DATETIME"), ("last_reviewed_by", "INTEGER")],
    "conflicts": [("proposed_keep_id", "INTEGER"), ("proposed_by", "INTEGER"),
                  ("proposed_note", "TEXT")],
    "qa_records": [("thread_of", "INTEGER")],
}

con = sqlite3.connect(DB)
cur = con.cursor()
for table, cols in ADD.items():
    existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
    for name, coltype in cols:
        if name in existing:
            print(f"  {table}.{name} already present, skipping")
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
        print(f"  added {table}.{name}")

# backfill: every existing document becomes its own owner (uploader) so
# "Request Update" has someone to route to
cur.execute("UPDATE documents SET owner_id = uploaded_by WHERE owner_id IS NULL")
con.commit()
con.close()
print("Migration done. New tables (folders, folder_items, ...) are created automatically the next "
      "time you start the server, and existing documents are re-split for better search on that first start.")
