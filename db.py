import os
import sqlite3
import json
import threading
from datetime import datetime
from typing import Dict, Any, Optional, Set, List

# Allow overriding the DB path via environment variable DB_PATH. Default to app.db
DEFAULT_DB_PATH = os.getenv('DB_PATH', 'C:\\Users\\<user>\\AppData\\Local\\linkedin-scraper\\app.db')

_lock = threading.Lock()

def _get_conn(db_path: Optional[str] = None):
    path = db_path or DEFAULT_DB_PATH
    # Ensure parent directory exists so sqlite can create the file there
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            # If we can't create the directory, let sqlite raise a useful error
            pass
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: Optional[str] = None):
    """Create tables if they don't exist.

    settings table: key (TEXT PRIMARY KEY), value (TEXT JSON)
    sent_jobs table: id (TEXT PRIMARY KEY), payload (TEXT JSON), created_at (TEXT)
    """
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            """)
            # Some SQLite builds don't support WITHOUT ROWID; ignore failures
            try:
                conn.commit()
            except Exception:
                conn.rollback()
                cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

            cur.execute("CREATE TABLE IF NOT EXISTS sent_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT)")
            conn.commit()
        finally:
            conn.close()

def get_settings(db_path: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            cur.execute('SELECT key, value FROM settings')
            rows = cur.fetchall()
            out: Dict[str, Any] = {}
            for r in rows:
                try:
                    out[r['key']] = json.loads(r['value'])
                except Exception:
                    out[r['key']] = r['value']
            return out
        finally:
            conn.close()

def save_settings(settings: Dict[str, Any], db_path: Optional[str] = None) -> bool:
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            for k, v in settings.items():
                val = json.dumps(v)
                cur.execute('INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, val))
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            conn.close()

def get_sent_job_ids(db_path: Optional[str] = None) -> Set[str]:
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            cur.execute('SELECT id FROM sent_jobs')
            rows = cur.fetchall()
            return set(r['id'] for r in rows)
        finally:
            conn.close()

def add_sent_job(job: Dict[str, Any], db_path: Optional[str] = None) -> bool:
    """Insert a job dict into sent_jobs, ignore if id already exists."""
    jid = job.get('id')
    if not jid:
        return False
    payload = json.dumps(job)
    created_at = datetime.utcnow().isoformat() + 'Z'
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO sent_jobs(id, payload, created_at) VALUES(?, ?, ?)', (jid, payload, created_at))
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            conn.close()

def get_all_sent_jobs(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn(db_path)
        try:
            cur = conn.cursor()
            cur.execute('SELECT payload FROM sent_jobs ORDER BY created_at ASC')
            rows = cur.fetchall()
            out = []
            for r in rows:
                try:
                    out.append(json.loads(r['payload']))
                except Exception:
                    pass
            return out
        finally:
            conn.close()


def db_info(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return resolved DB path and whether it looks like it's inside OneDrive.

    Returns: {'path': <abs path>, 'onedrive': bool}
    """
    path = db_path or DEFAULT_DB_PATH
    abs_path = os.path.abspath(path)
    onedrive = False
    # check OneDrive environment variables first
    one_env = os.getenv('OneDrive') or os.getenv('ONEDRIVE')
    try:
        if one_env:
            try:
                if os.path.commonpath([abs_path, os.path.abspath(one_env)]) == os.path.abspath(one_env):
                    onedrive = True
            except Exception:
                pass
        # heuristic: path contains 'onedrive' segment
        if not onedrive:
            p = abs_path.lower()
            if '\\onedrive\\' in p or '/onedrive/' in p:
                onedrive = True
    except Exception:
        onedrive = False
    return {'path': abs_path, 'onedrive': onedrive}
