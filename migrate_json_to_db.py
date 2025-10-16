#!/usr/bin/env python3
"""Migration helper: import existing JSON files into the app's SQLite DB.

- Reads `settings.json` and merges into DB (overwrites top-level keys).
- Reads `sent-jobs.json` and `sent-jobs.json.bak`, adds each job id into sent_jobs table.
- Prints summary and safe-delete commands for the JSON files.
"""
import json
import os
import sys
from pathlib import Path

# Try import db helper from repo
try:
    import db
except Exception:
    print("Error: cannot import local db module. Run this from the project root where db.py exists.")
    raise

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / 'settings.json'
SENT_JOBS = ROOT / 'sent-jobs.json'
SENT_BAK = ROOT / 'sent-jobs.json.bak'


def load_json(path):
    if not path.exists():
        return None
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def main():
    db_path = os.environ.get('DB_PATH') or db.DEFAULT_DB_PATH
    print(f"Using DB: {db_path}")
    db.init_db(db_path)

    migrated = {'settings': 0, 'sent_jobs': 0}

    # Settings
    sdata = load_json(SETTINGS)
    if sdata is None:
        print(f"No {SETTINGS.name} found — skipping settings import.")
    else:
        # Current settings in DB
        cur = db.get_settings(db_path) or {}
        # Merge top-level keys (overwrite existing with file values)
        cur.update(sdata)
        db.save_settings(cur, db_path)
        migrated['settings'] = len(sdata.keys()) if isinstance(sdata, dict) else 1
        print(f"Imported settings keys: {list(sdata.keys())}")

    # Sent jobs
    def import_sent(path):
        data = load_json(path)
        if not data:
            return 0
        count = 0
        for entry in data:
            # each entry expected to be a dict with an 'id'
            if not isinstance(entry, dict):
                continue
            jid = entry.get('id')
            if not jid:
                continue
            try:
                # pass the full entry dict so db.add_sent_job can store payload
                db.add_sent_job(entry, db_path)
                count += 1
            except Exception as e:
                print(f"Warning: failed to add sent job {jid}: {e}")
        return count

    c1 = import_sent(SENT_JOBS)
    c2 = import_sent(SENT_BAK)
    migrated['sent_jobs'] = c1 + c2

    print("\nMigration summary:")
    print(f" settings migrated: {migrated['settings']}")
    print(f" sent jobs added: {migrated['sent_jobs']}")

    print("\nSafety: keep a copy of the original JSON files for 24h before deleting. To delete now run:")
    cmds = []
    for p in (SENT_JOBS, SETTINGS, SENT_BAK):
        if p.exists():
            cmds.append(f"Remove-Item -Path '{p.resolve()}' -Force")
    if cmds:
        print('\n'.join(cmds))
    else:
        print("No JSON files found — nothing to delete.")


if __name__ == '__main__':
    main()
