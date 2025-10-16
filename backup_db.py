import shutil
import os
from datetime import datetime

from db import db_info

"""Simple backup script for the app SQLite database.

Usage:
    python backup_db.py

This copies the resolved DB file to the same directory with a timestamp suffix.
"""

if __name__ == '__main__':
    info = db_info()
    src = info['path']
    if not os.path.exists(src):
        print('Source DB not found:', src)
        raise SystemExit(1)
    dst_dir = os.path.dirname(src)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    dst = os.path.join(dst_dir, f'app.db.backup.{ts}')
    shutil.copy2(src, dst)
    print('Backup created:', dst)
