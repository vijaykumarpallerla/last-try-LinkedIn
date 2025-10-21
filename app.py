import os
import json
import smtplib
import time
import threading
import logging
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify, send_file
from functools import wraps
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime, timedelta
import re
from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException, NoSuchElementException
import hashlib
import tempfile
import uuid
from urllib.parse import quote_plus
import requests
import difflib
import gzip
import base64
import shutil
import sys

# --- CONFIGURATION ---
SENT_JOBS_FILE = 'sent-jobs.json'
EXTRACTED_EMAILS_FILE = 'extracted-emails.json'
HOURS_LOOKBACK = 6
SETTINGS_FILE = 'settings.json'

# Try to load environment variables from a .env file if present.
def _load_dotenv_optional(env_path='.env'):
    """Load .env into os.environ if python-dotenv available; else do a simple parse.

    Keeps behavior forgiving so the app can run even if python-dotenv isn't installed.
    """
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        logging.getLogger(__name__).info('Loaded environment from .env via python-dotenv')
        return
    except Exception:
        # Manual parse fallback
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"\''))
            logging.getLogger(__name__).info('Loaded environment from .env via manual parser')
        except Exception:
            logging.getLogger(__name__).exception('Failed to parse .env')


_load_dotenv_optional()

# Helper: on Windows, try reading Chrome version from registry when --version output is unreliable
def get_windows_chrome_version():
    try:
        import winreg
    except Exception:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in (r"SOFTWARE\\Google\\Chrome\\BLBeacon", r"SOFTWARE\\WOW6432Node\\Google\\Chrome\\BLBeacon"):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    try:
                        val, _ = winreg.QueryValueEx(k, 'version')
                        if val:
                            return val
                    except Exception:
                        pass
            except Exception:
                pass
    return None


def is_chrome_running_on_windows():
    """Return True if any chrome.exe processes are running on Windows."""
    try:
        if not sys.platform.startswith('win'):
            return False
        # Use tasklist to detect chrome.exe processes
        out = os.popen('tasklist /FI "IMAGENAME eq chrome.exe" /NH').read()
        if not out:
            return False
        # tasklist returns a line with 'INFO: No tasks are running which match the specified criteria.' when none
        if 'No tasks are running' in out or 'INFO:' in out:
            return False
        # Otherwise presence of 'chrome.exe' indicates running processes
        return 'chrome.exe' in out.lower()
    except Exception:
        return False

# Create Flask app
app = Flask(__name__)

from db import (
    init_db,
    get_settings as db_get_settings,
    save_settings as db_save_settings,
    get_sent_job_ids,
    add_sent_job,
    get_all_sent_jobs,
)

# Database initialization will be performed after logging is configured farther down

# Settings helpers (delegates to db.py by default)
def load_settings():
    try:
        db_settings = db_get_settings() or {}
        # If a local settings.json exists, merge in any missing keys (e.g., groups) and persist back to DB.
        file_settings = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    file_settings = json.load(f)
            except Exception:
                logger.warning('Could not parse settings.json while merging with DB settings')
        changed = False
        # Generic merge: fill missing keys
        for k, v in (file_settings or {}).items():
            if k == 'groups':
                continue  # handle groups specially below
            # Only merge keys that are missing or empty in DB
            if k not in db_settings or (not db_settings.get(k)):
                db_settings[k] = v
                changed = True

        # Special-case merge for groups: only import from file if DB has no 'groups' key at all.
        # If DB has an explicit empty list (user-cleared), DO NOT re-import from file.
        try:
            file_groups = file_settings.get('groups') or []
            has_db_groups_key = ('groups' in db_settings)
            if (not has_db_groups_key) and isinstance(file_groups, list) and file_groups:
                # Normalize
                def norm(gs):
                    out = []
                    for g in gs:
                        if isinstance(g, dict) and g.get('url'):
                            out.append({'url': g.get('url'), 'name': g.get('name') or g.get('url')})
                    return out
                db_settings['groups'] = norm(file_groups)
                changed = True
        except Exception:
            logger.exception('Failed merging groups from settings.json')
        if changed:
            try:
                db_save_settings(db_settings)
            except Exception:
                logger.exception('Failed to save merged settings into DB')
        return db_settings
    except Exception:
        logger.exception('DB settings read failed; falling back to file')
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                logger.warning('Could not read settings.json; using defaults')
        return {}

def save_settings(settings: dict):
    try:
        ok = db_save_settings(settings)
        return ok
    except Exception:
        logger.exception('DB save settings failed; falling back to file write')
        try:
            dirpath = os.path.dirname(os.path.abspath(SETTINGS_FILE)) or '.'
            fd, tmp = tempfile.mkstemp(prefix='settings-', dir=dirpath, text=True)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(settings, f, indent=2)
                os.replace(tmp, SETTINGS_FILE)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
            return True
        except Exception:
            logger.exception('Failed to save settings to file')
            return False


def write_json_atomic(path: str, data):
    """Write data (JSON-serializable) to path atomically on Windows and POSIX.

    This writes to a temporary file in the same directory and then os.replace()
    to avoid partially-written files which can lead to JSONDecodeError later.
    """
    dirpath = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='tmp-', dir=dirpath, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

# --- ID STABILITY HELPERS ---
def _normalize_text_for_id(text: str) -> str:
    """Normalize post text to reduce duplicate emails from minor UI/time changes.

    - Lowercase
    - Remove 'recent time' words like '2h', '5 hr', '10m', 'just now'
    - Remove common social counters like '123 likes', '45 comments', 'share'
    - Collapse whitespace
    """
    if not text:
        return ''
    t = text.lower()
    try:
        # remove time words
        t = re.sub(r"\b\d+\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b", " ", t)
        t = re.sub(r"\bjust now\b", " ", t)
        # remove interaction counters/labels
        t = re.sub(r"\b\d+\s+likes?\b", " ", t)
        t = re.sub(r"\b\d+\s+comments?\b", " ", t)
        t = re.sub(r"\bshares?\b", " ", t)
        t = re.sub(r"\bfollow(ing)?\b", " ", t)
        t = re.sub(r"\bpremium\b", " ", t)
        # collapse whitespace
        t = re.sub(r"\s+", " ", t).strip()
    except Exception:
        t = text.strip().lower()
    return t

def _extract_linkedin_activity_id_from_anchors(anchors: list) -> str | None:
    """Try to extract a stable LinkedIn activity/update ID from anchor hrefs.
    Returns an id string like 'activity:1234567890' or None.
    """
    if not anchors:
        return None
    try:
        for a in anchors:
            try:
                href = (a.get_attribute('href') or '')
            except Exception:
                href = ''
            if not href:
                continue
            h = href.lower()
            if 'activity:' in h:
                # urn:li:activity:12345
                try:
                    idx = h.index('activity:')
                    part = h[idx:]
                    # keep up to next non-digit boundary
                    m = re.search(r"activity:(\d+)", part)
                    if m:
                        return f"activity:{m.group(1)}"
                except Exception:
                    pass
            # Some links look like /feed/update/urn:li:activity:1234 or contain '/updates/' with share id
            if '/feed/update/' in h and 'activity:' in h:
                try:
                    m = re.search(r"activity:(\d+)", h)
                    if m:
                        return f"activity:{m.group(1)}"
                except Exception:
                    pass
            if '/posts/' in h:
                # As a fallback, hash the posts URL path (stable enough)
                try:
                    from urllib.parse import urlparse
                    p = urlparse(h)
                    if p.path:
                        return f"postpath:{hashlib.sha1(p.path.encode('utf-8')).hexdigest()}"
                except Exception:
                    pass
    except Exception:
        return None
    return None


def extract_role_from_text(text: str) -> str | None:
    """Heuristic extraction of a role/title from post text.

    Returns a short role string like 'Python Developer' or 'Senior Software Engineer', or None.
    This is intentionally conservative: we try a few regexes in order and return the first reasonable match.
    """
    if not text:
        return None
    t = text.replace('\n', ' ').strip()
    try:
        # 1) Look for explicit phrases like 'looking for', 'hiring', 'seeking' followed by a title
        m = re.search(r"(?:looking for|we're looking for|we are looking for|hiring|we're hiring|we are hiring|seeking|open for|open role for)\s+(?:an?|the)?\s*([A-Za-z0-9+.#\s\-]{3,80}?)\b(?:\.|,|\band|for|in|\(|$)", t, re.I)
        if m:
            candidate = m.group(1).strip(' .,:;\\/')
            if 3 <= len(candidate) <= 80:
                return ' '.join(candidate.split())

        # 2) Role keywords e.g. 'Python Developer', 'Java Engineer', 'Full Stack Developer'
        # Capture up to 4 words ending with a role keyword
        role_kw = r"(?:developer|engineer|manager|designer|architect|consultant|analyst|specialist|lead|scientist|administrator|admin|devops|full[ -]?stack|backend|frontend|mobile|engineer)"
        m2 = re.search(rf"([A-Za-z0-9+#\.\-\s]{{0,60}}\b{role_kw})", t, re.I)
        if m2:
            candidate = m2.group(1).strip(' .,:;\\/')
            if 3 <= len(candidate) <= 80:
                return ' '.join(candidate.split())

        # 3) Fallback: look for common language + role combos like 'Python', 'Java' near the word 'developer' elsewhere
        m3 = re.search(r"(Python|JavaScript|Java|Go|Golang|Ruby|C\+\+|C#|Node|React|Django|Flask)\s+([A-Za-z]{2,20})", t, re.I)
        if m3:
            candidate = (m3.group(1) + ' ' + m3.group(2)).strip()
            return candidate
    except Exception:
        pass
    return None

# configure basic logging so terminal shows progress
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Admin token for protecting admin endpoints. If not set, admin endpoints remain unprotected but a warning is logged.
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN') or 'ChangeThisToAStrongSecret'

# AI (Gemini) configuration via environment
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = os.getenv('GEMINI_API_URL')  # e.g., https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent
GEMINI_API_VERSION = os.getenv('GEMINI_API_VERSION')  # optional override, e.g. 'v1' or 'v1beta'
AI_FILTER_ENABLED_DEFAULT = os.getenv('AI_FILTER_ENABLED', 'true').lower() in ('1', 'true', 'yes')

def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        # If no ADMIN_TOKEN configured, allow but warn
        if not ADMIN_TOKEN:
            logger.warning('ADMIN_TOKEN not set; admin endpoints are unprotected')
            return f(*args, **kwargs)
        # Token may be supplied via header X-Admin-Token or query param admin_token
        token = request.headers.get('X-Admin-Token') or request.args.get('admin_token')
        if token == ADMIN_TOKEN:
            return f(*args, **kwargs)
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    return wrapped

# Initialize database on startup (creates tables if necessary)
try:
    init_db()
    try:
        from db import db_info
        info = db_info()
        logger.info(f"Database initialized at: {info['path']}")
        if info.get('onedrive'):
            logger.warning('Database path appears to be inside OneDrive. Consider moving DB to a local folder (e.g., %LOCALAPPDATA%) to avoid cloud sync of sensitive data.')
    except Exception:
        logger.info('Database initialized (app.db)')
except Exception:
    logger.exception('Failed to initialize database; falling back to JSON files')

# --- GLOBAL STATUS ---
# This dictionary will hold the status of the running scraper task
scraper_status = {
    'is_running': False,
    'progress': 'Idle. Ready to start.'
}

# Flags used for human verification handoff/resume
scraper_status.setdefault('paused_for_human_verification', False)
scraper_status.setdefault('resume_requested', False)
scraper_status.setdefault('ai_filter_enabled', AI_FILTER_ENABLED_DEFAULT)
scraper_status.setdefault('ai_filter_stats', {'kept': 0, 'skipped': 0, 'errors': 0})
scraper_status.setdefault('extracted_emails_count', 0)
scraper_status.setdefault('extracted_emails_file', '')

# Paused sessions mapping: token -> {'driver': selenium.webdriver, 'created_at': timestamp, 'expires': ts}
# Stored in-memory only; tokens are short-lived.
paused_sessions = {}

def _make_token():
    return uuid.uuid4().hex

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _save_artifacts(driver, token: str):
    # Save screenshot and html.gz paths for this token
    ss_path = os.path.join('data', 'screenshots', f'{token}.png')
    html_path = os.path.join('data', 'html', f'{token}.html.gz')
    _ensure_dir(ss_path)
    _ensure_dir(html_path)
    try:
        driver.save_screenshot(ss_path)
    except Exception:
        try:
            # fallback to executing JS to get a base64 screenshot (Playwright alternative) if available
            b64 = driver.get_screenshot_as_base64()
            with open(ss_path, 'wb') as f:
                f.write(base64.b64decode(b64))
        except Exception:
            pass
    try:
        html = driver.page_source
        with gzip.open(html_path, 'wt', encoding='utf-8') as gz:
            gz.write(html)
    except Exception:
        try:
            html = ''
            with gzip.open(html_path, 'wt', encoding='utf-8') as gz:
                gz.write(html)
        except Exception:
            pass
    return ss_path, html_path


def _save_live_screenshot(driver):
    """Save a single live screenshot and gzipped HTML to a known path for UI live view.

    This writes to data/live.png and data/live.html.gz (overwriting previous).
    Returns (png_path, html_path) or (None, None) on failure.
    """
    try:
        png = os.path.join('data', 'live.png')
        htmlp = os.path.join('data', 'live.html.gz')
        _ensure_dir(png)
        _ensure_dir(htmlp)
        try:
            driver.save_screenshot(png)
        except Exception:
            try:
                b64 = driver.get_screenshot_as_base64()
                with open(png, 'wb') as f:
                    f.write(base64.b64decode(b64))
            except Exception:
                return None, None
        try:
            page = driver.page_source or ''
            with gzip.open(htmlp, 'wt', encoding='utf-8') as gz:
                gz.write(page)
        except Exception:
            try:
                with gzip.open(htmlp, 'wt', encoding='utf-8') as gz:
                    gz.write('')
            except Exception:
                pass
        return png, htmlp
    except Exception:
        logger.exception('Failed saving live screenshot')
        return None, None

def _send_email_simple(to_addrs: list | str, subject: str, body: str, attachments: list = None):
    # Simple SMTP send using GMAIL_USER/GMAIL_PASS or SMTP_* env vars
    gmail_user = os.getenv('GMAIL_USER')
    gmail_pass = os.getenv('GMAIL_PASS')
    if isinstance(to_addrs, str):
        to_list = [r.strip() for r in to_addrs.split(',') if r.strip()]
    else:
        to_list = to_addrs or []
    if not to_list:
        logger.warning('No recipients for _send_email_simple')
        return False
    if not gmail_user or not gmail_pass:
        logger.warning('_send_email_simple: SMTP credentials not configured')
        return False
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = gmail_user
        msg['To'] = ', '.join(to_list)
        server = smtplib.SMTP_SSL(os.getenv('SMTP_HOST') or 'smtp.gmail.com', int(os.getenv('SMTP_PORT') or 465), timeout=20)
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, to_list, msg.as_string())
        server.quit()
        return True
    except Exception:
        logger.exception('Failed to send notification email')
        return False


# Global stop signal for immediate user-requested cancellation
stop_event = threading.Event()

class StopRequested(Exception):
    """Raised internally to unwind the scraper when a stop is requested."""
    pass

def _assert_not_stopped():
    if stop_event.is_set():
        raise StopRequested()

def _resolve_gemini_model_url(prefer: str = 'flash'):
    """Call ListModels to find a working model that supports generateContent.
    prefer: 'flash' or 'pro' determines priority when picking among available models.
    Returns: (resolved_url, info_message) or (None, reason) if not found.
    """
    if not GEMINI_API_KEY:
        return None, 'no-api-key'

    base = 'https://generativelanguage.googleapis.com'
    # Build version preference order
    versions = []
    if GEMINI_API_VERSION:
        versions.append(GEMINI_API_VERSION)
    for v in ['v1', 'v1beta']:
        if v not in versions:
            versions.append(v)

    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    last_err = None
    for ver in versions:
        try:
            resp = requests.get(f"{base}/{ver}/models", params=params, headers=headers, timeout=15)
        except Exception as e:
            last_err = f"http-ex:{type(e).__name__}:{str(e)[:120]}"
            continue
        if resp.status_code != 200:
            last_err = f"http-{resp.status_code}:{resp.text[:180]}"
            continue
        data = resp.json() or {}
        models = data.get('models') or data.get('model') or []
        if not isinstance(models, list):
            last_err = 'no-models-list'
            continue

        def supports_generate(m):
            methods = m.get('supportedGenerationMethods') or m.get('supported_methods') or []
            return 'generateContent' in methods if isinstance(methods, list) else False

        candidates = [m for m in models if supports_generate(m)]
        if not candidates:
            last_err = 'no-generateContent-models'
            continue

        def score(m):
            name = m.get('name', '')
            s = 0
            if '1.5' in name:
                s += 10
            if prefer == 'flash' and 'flash' in name:
                s += 5
            if prefer == 'pro' and 'pro' in name:
                s += 5
            if name.endswith('-latest'):
                s += 3
            if any(x in name for x in ['-002', '-003']):
                s += 2
            if any(x in name for x in ['vision', 'audio', 'exp']):
                s -= 1
            return s

        best = max(candidates, key=score)
        best_name = best.get('name')
        if best_name:
            # best_name is typically like 'models/gemini-1.5-flash' in v1 API
            path = best_name if best_name.startswith('models/') else f"models/{best_name}"
            resolved = f"{base}/{ver}/{path}:generateContent"
            logger.info(f"Resolved Gemini model via ListModels: {best_name} on {ver}")
            return resolved, f"resolved:{best_name}:{ver}"

    return None, (last_err or 'resolve-failed')

def ai_is_usa_hiring_post(text: str, timeout: int = 20) -> tuple[bool, str]:
    global GEMINI_API_URL
    """Call Gemini to determine if a post is about hiring in the USA.

    Returns (keep, reason). keep=True if it's hiring and US-related.
    """
    if not GEMINI_API_KEY or not GEMINI_API_URL:
        # If not configured, default to keep (no AI gate)
        return True, 'ai-disabled'
    prompt = (
        "You are a precise filter for job posts. Read the text and answer strictly in JSON with keys: "
        "{\"hiring\": true|false, \"usa\": true|false, \"reason\": \"short reason\"}. "
        "- hiring=true only if the post is recruiting/hiring or contains openings/positions/vacancies/looking for candidates (not job seeking). "
        "- usa=true only if the role is in the United States of America (50 states or DC), or remote but explicitly restricted to US residents. "
        "  Treat US territories (e.g., Puerto Rico, Guam, USVI) and other countries as NOT usa. If global with no explicit US-only restriction, set usa=false. "
        "Text: " + text[:4000]
    )
    try:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }
        # Only add key as query param if the GEMINI_API_URL doesn't already include it
        params = {}
        if 'key=' not in (GEMINI_API_URL or ''):
            params = {"key": GEMINI_API_KEY}
        headers = {"Content-Type": "application/json"}
        resp = requests.post(GEMINI_API_URL, params=params, headers=headers, json=payload, timeout=timeout)
        try:
            resp.raise_for_status()
        except requests.HTTPError as http_err:
            code = getattr(http_err.response, 'status_code', None)
            msg = ''
            try:
                errj = http_err.response.json()
                msg = (errj.get('error') or {}).get('message') or ''
            except Exception:
                pass
            # If 404 model not found/unsupported, try to resolve a supported model and retry once
            if code == 404:
                # 1) Resolve a working model for this key (preferring flash)
                try:
                    resolved_url, why = _resolve_gemini_model_url(prefer='flash')
                except Exception:
                    resolved_url, why = None, 'resolve-exception'
                for candidate_url in [resolved_url, None]:
                    try_url = candidate_url or None
                    # If resolution failed, try simply swapping API version v1 <-> v1beta
                    if not try_url:
                        try:
                            import re as _re3
                            if '/v1/' in GEMINI_API_URL:
                                try_url = _re3.sub(r"/v1/", "/v1beta/", GEMINI_API_URL)
                            elif '/v1beta/' in GEMINI_API_URL:
                                try_url = _re3.sub(r"/v1beta/", "/v1/", GEMINI_API_URL)
                        except Exception:
                            try_url = None
                    if not try_url or try_url == GEMINI_API_URL:
                        continue
                    try:
                        r2 = requests.post(try_url, params=params, headers=headers, json=payload, timeout=timeout)
                        r2.raise_for_status()
                        data2 = r2.json()
                        out_text2 = ''
                        try:
                            out_text2 = data2['candidates'][0]['content']['parts'][0]['text']
                        except Exception:
                            pass
                        cleaned2 = (out_text2 or '').strip()
                        if cleaned2.startswith('```'):
                            cleaned2 = cleaned2.strip('`').replace('json\n', '').replace('json\r\n', '')
                        try:
                            obj2 = json.loads(cleaned2)
                        except Exception:
                            import re as _re4
                            m2 = _re4.search(r"\{[\s\S]*\}", cleaned2)
                            obj2 = json.loads(m2.group(0)) if m2 else None
                        if isinstance(obj2, dict):
                            hiring = bool(obj2.get('hiring'))
                            usa = bool(obj2.get('usa'))
                            reason2 = str(obj2.get('reason') or '')
                            # Update global URL so future requests use the working endpoint
                            try:
                                GEMINI_API_URL = try_url
                            except Exception:
                                pass
                            return (hiring and usa), reason2 or ('hiring=%s usa=%s' % (hiring, usa))
                    except Exception:
                        continue
            reason = f"ai-error:http-{code}" + (f" {msg}" if msg else '')
            logger.warning(f"AI filter error: {reason}")
            return True, reason.strip()
        data = resp.json()
        # Extract the model text
        out_text = ''
        try:
            out_text = data['candidates'][0]['content']['parts'][0]['text']
        except Exception:
            pass
        # Parse JSON from the model output; handle if it returned code fences
        cleaned = out_text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`')
            # possible leading json word
            cleaned = cleaned.replace('json\n', '').replace('json\r\n', '')
        try:
            obj = json.loads(cleaned)
        except Exception:
            # try to locate JSON inside
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", cleaned)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except Exception:
                    obj = None
            else:
                obj = None
        if not isinstance(obj, dict):
            return True, 'ai-parse-failed'
        hiring = bool(obj.get('hiring'))
        usa = bool(obj.get('usa'))
        reason = str(obj.get('reason') or '')
        return (hiring and usa), reason or ('hiring=%s usa=%s' % (hiring, usa))
    except Exception as e:
        emsg = str(e)
        # Avoid leaking full URLs with query params
        if 'http' in emsg and '?' in emsg:
            emsg = emsg.split('?', 1)[0]
        reason = f"ai-error:{emsg[:120]}"
        logger.warning(f"AI filter error: {reason}")
        return True, reason

# The main function that does all the work, adapted for Flask
def scraper_task(gmail_user, gmail_pass, recipient_emails, linkedin_user, linkedin_pass, delay_seconds=10, send_separately=True, groups=None, keywords=None, require_keywords=False, use_keywords_search=False, hold_emails_only=False):
    """This function runs in a separate thread to avoid blocking the web server."""
    global scraper_status
    scraper_status['is_running'] = True
    scraper_status['progress'] = 'Starting scraper...'
    logger.info('Scraper: Starting scraper...')

    # Clear any previous stop request when starting a fresh run
    stop_event.clear()

    try:
        # --- Selenium Setup ---
        _assert_not_stopped()
        scraper_status['progress'] = 'Setting up browser...'
        logger.info('Scraper: Setting up browser...')
        chrome_options = Options()
        # When running in a container/headless environment we need several flags so
        # Chromium can start reliably (Render, Docker, CI). Allow override via HEADLESS env.
        headless_env = os.getenv('HEADLESS', 'true').lower() in ('1', 'true', 'yes')
        if headless_env:
            # Newer Chrome supports --headless=new; fallback to --headless
            try:
                chrome_options.add_argument("--headless=new")
            except Exception:
                chrome_options.add_argument("--headless")
        # Platform-specific Chrome flags: minimal on Windows, fuller set in containers/Linux
        if sys.platform.startswith('win'):
            # Windows: prefer minimal options to avoid crashes when launching headless Chrome
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
        else:
            # Non-Windows (Linux/macOS/containers): use more robust flags for containerized Chrome
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-software-rasterizer")
            chrome_options.add_argument("--remote-debugging-port=9222")
            chrome_options.add_argument("--window-size=1920,1080")
            # Some platforms benefit from this to avoid zygote permission issues
            chrome_options.add_argument("--no-zygote")
            chrome_options.add_argument("--single-process")
        # Use an isolated temporary profile to ensure Chrome starts as a separate
        # instance instead of opening a tab in the user's existing browser.
        try:
            profile_dir = tempfile.mkdtemp(prefix='linkedin-scraper-profile-')
            chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        except Exception:
            profile_dir = None
        # Reduce automation flags noise
        try:
            chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
        except Exception:
            pass

        # Try to find Chrome/Chromium binary. Prefer an explicit env var, then common Linux and Windows paths.
        chrome_bin_env = os.getenv('CHROME_BIN') or os.getenv('GOOGLE_CHROME_BIN') or os.getenv('CHROME_PATH')
        chrome_found = False
        tried_candidates = []
        if chrome_bin_env:
            tried_candidates.append(('env', chrome_bin_env))
            # Sometimes the env var may point to a symlink or non-exact path; try shutil.which on the basename too
            try:
                if os.path.exists(chrome_bin_env) or os.access(chrome_bin_env, os.X_OK):
                    chrome_options.binary_location = chrome_bin_env
                    logger.info(f"Scraper: Using CHROME_BIN from env: {chrome_bin_env}")
                    chrome_found = True
                else:
                    # try resolving by name
                    bn = os.path.basename(chrome_bin_env)
                    resolved = shutil.which(bn) or ''
                    if resolved and os.path.exists(resolved):
                        chrome_options.binary_location = resolved
                        logger.info(f"Scraper: Resolved CHROME_BIN name '{bn}' to {resolved}")
                        chrome_found = True
                    else:
                        logger.info(f"Scraper: CHROME_BIN env set to '{chrome_bin_env}' but file not found or not executable")
            except Exception:
                logger.exception('Error while evaluating CHROME_BIN')

        # Platform-agnostic lookups (Linux-first, then Windows). Use shutil.which where possible.
        if not chrome_found:
            # Common linux binary names and explicit paths (keep order of preference)
            linux_candidates = [
                shutil.which('google-chrome-stable') or '',
                shutil.which('google-chrome') or '',
                shutil.which('chromium-browser') or '',
                shutil.which('chromium') or '',
                '/usr/bin/google-chrome',
                '/usr/bin/chromium-browser',
                '/usr/bin/chromium',
                '/snap/bin/chromium'
            ]
            for p in linux_candidates:
                if not p:
                    continue
                tried_candidates.append(('candidate', p))
                try:
                    if os.path.exists(p) and os.access(p, os.X_OK):
                        chrome_options.binary_location = p
                        logger.info(f"Scraper: Found Chrome/Chromium binary at {p}")
                        chrome_found = True
                        break
                except Exception:
                    logger.exception(f'Error while checking candidate path: {p}')

        # If still not found, log what we tried to help diagnostics
        if not chrome_found:
            try:
                logger.info('Scraper: Chrome detection tried the following candidates: %s', tried_candidates)
            except Exception:
                pass

        # Fall back to checking common Windows locations (for local Windows dev)
        if not chrome_found:
            chrome_paths = [
                r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
            ]
            for p in chrome_paths:
                if os.path.exists(p):
                    chrome_options.binary_location = p
                    logger.info(f"Scraper: Found Chrome binary at {p}")
                    chrome_found = True
                    break

        if not chrome_found:
            # Give a clear message both in terminal and UI status
            msg = (
                "Chrome/Chromium browser binary not found. Please install Google Chrome/Chromium or set the path via the CHROME_BIN env var. "
                "On Linux, common paths are /usr/bin/google-chrome or /usr/bin/chromium-browser."
            )
            logger.error('Scraper: ' + msg)
            scraper_status['progress'] = msg
            scraper_status['is_running'] = False
            return

        # Choose chromedriver: prefer system-installed, else use webdriver-manager
        chromedriver_path = None
        service = None
        try:
            system_cd = shutil.which('chromedriver') or '/usr/bin/chromedriver'
            if system_cd and os.path.exists(system_cd) and os.access(system_cd, os.X_OK):
                chromedriver_path = system_cd
                service = Service(executable_path=chromedriver_path)
                logger.info(f"Scraper: Using system chromedriver at {chromedriver_path}")
                try:
                    out = os.popen(f'"{chromedriver_path}" --version').read().strip()
                    if out:
                        logger.info(f"Scraper: chromedriver version: {out}")
                except Exception:
                    pass
            else:
                # Detect browser version and request a matching chromedriver
                browser_bin = chrome_options.binary_location or shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
                browser_version = None
                if browser_bin and os.path.exists(browser_bin):
                    try:
                        # On Windows calling chrome.exe --version can open a browser window
                        # (which previously caused an extra empty tab). Prefer reading from
                        # the registry when on Windows to avoid side effects.
                        if sys.platform.startswith('win'):
                            out = get_windows_chrome_version() or ''
                        else:
                            out = os.popen(f'"{browser_bin}" --version').read().strip()
                        logger.info(f"Scraper: Detected browser version output: {out}")
                        # Ignore spurious messages that indicate Chrome opened a UI
                        if out and 'opening in existing' in out.lower():
                            out = ''
                        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
                        if m:
                            browser_version = m.group(1)
                        else:
                            m2 = re.search(r"(\d+)\.", out)
                            if m2:
                                browser_version = m2.group(1)
                    except Exception:
                        logger.exception('Failed to read browser version')

                try:
                    # webdriver-manager changed its constructor signature in some
                    # versions. Try to call with 'version' first, otherwise fall
                    # back to the no-arg constructor and rely on the returned
                    # path from install().
                    try:
                        if browser_version:
                            chromedriver_path = ChromeDriverManager(version=browser_version).install()
                        else:
                            chromedriver_path = ChromeDriverManager().install()
                    except TypeError:
                        # Older/newer webdriver-manager may expect no 'version'
                        # kwarg; try the maker without kwargs and pass a
                        # matching_version param to install if available.
                        mgr = ChromeDriverManager()
                        try:
                            # try install with matching_version param
                            if browser_version:
                                chromedriver_path = mgr.install(matching_version=browser_version)
                            else:
                                chromedriver_path = mgr.install()
                        except TypeError:
                            # Last resort: call install() without matching
                            chromedriver_path = mgr.install()
                    service = Service(executable_path=chromedriver_path)
                    logger.info(f"Scraper: Using chromedriver at {chromedriver_path}")
                except Exception:
                    logger.exception('Scraper: webdriver-manager failed; falling back to PATH for chromedriver')
                    service = Service()
        except Exception:
            logger.warning('Scraper: chromedriver detection failed; falling back to PATH')
            service = Service()

        _assert_not_stopped()

        # On developer Windows machines, an already-running Chrome process can cause the driver
        # to attach to the user's browser (opening a normal tab). If the user explicitly requests
        # headless mode (HEADLESS=true) allow the scraper to proceed; otherwise abort with a helpful
        # message so the user can either close Chrome or set HEADLESS=true.
        # Allow a forced UI override (risky) via env FORCE_UI=true to bypass this check
        force_ui = os.getenv('FORCE_UI', '').lower() in ('1', 'true', 'yes')
        logger.info('Scraper: headless_env=%s force_ui=%s', headless_env, force_ui)
        if not headless_env and is_chrome_running_on_windows() and not force_ui:
            msg = 'Chrome is currently running on this machine. Close Chrome or set HEADLESS=true to run the scraper.'
            logger.error('Scraper: ' + msg)
            scraper_status['progress'] = msg
            scraper_status['is_running'] = False
            return
        if force_ui:
            logger.warning('Scraper: FORCE_UI enabled - attempting visible UI run despite running Chrome (may attach to existing profile)')

        # Helper to create a fresh webdriver instance
        def create_driver():
            try:
                _assert_not_stopped()
                drv = webdriver.Chrome(service=service, options=chrome_options)
                logger.info('Scraper: New WebDriver instance created, session id=%s', getattr(drv, 'session_id', None))
                return drv
            except Exception:
                logger.exception('Scraper: Failed to create WebDriver')
                raise

        # Helper to perform login steps into LinkedIn on a given driver instance
        def do_login(drv):
            try:
                _assert_not_stopped()
                drv.get('https://www.linkedin.com/login')
                time.sleep(2)
                retry_on_stale(lambda: drv.find_element(By.ID, 'username').clear())
                retry_on_stale(lambda: drv.find_element(By.ID, 'username').send_keys(linkedin_user))
                retry_on_stale(lambda: drv.find_element(By.ID, 'password').clear())
                retry_on_stale(lambda: drv.find_element(By.ID, 'password').send_keys(linkedin_pass))
                retry_on_stale(lambda: drv.find_element(By.XPATH, '//*[@type="submit"]').click())
                time.sleep(5)
            except Exception:
                logger.exception('Scraper: Exception during login')
                raise

        # Safe get helper: tries driver.get and recreates driver+login on invalid session
        def safe_get(drv, url, attempts=2):
            for attempt in range(attempts):
                try:
                    _assert_not_stopped()
                    drv.get(url)
                    return drv
                except Exception as e:
                    # If session invalid, try to recreate and re-login
                    from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
                    if isinstance(e, InvalidSessionIdException) or 'invalid session id' in str(e).lower() or isinstance(e, WebDriverException):
                        logger.warning('Scraper: Detected invalid webdriver session; attempting to recreate (attempt %s/%s)', attempt+1, attempts)
                        try:
                            try:
                                drv.quit()
                            except Exception:
                                pass
                            drv = create_driver()
                            do_login(drv)
                            continue
                        except Exception:
                            logger.exception('Scraper: Failed to recreate driver during safe_get')
                            raise
                    else:
                        raise
            # If we exit loop, raise
            raise RuntimeError('safe_get: failed to load url after retries')

        driver = create_driver()
        # Save an initial live screenshot so the UI can show something
        try:
            _save_live_screenshot(driver)
        except Exception:
            pass
        # Some ChromeDriver/Chrome combinations open an extra initial blank tab
        # (about:blank or data:,). Give Chrome a short moment to populate handles
        # and then close any truly empty startup tabs so the user sees the real
        # controlled tab. Be conservative to avoid closing legitimate pages.
        try:
            time.sleep(0.35)
            handles = list(driver.window_handles)
            if len(handles) > 1:
                for h in list(handles):
                    try:
                        driver.switch_to.window(h)
                        cur = (driver.current_url or '').strip()
                    except Exception:
                        cur = ''
                    if not cur or cur in ('about:blank', 'data:,'):
                        logger.info('Scraper: Closing empty startup tab (url=%s)', cur)
                        try:
                            driver.close()
                        except Exception:
                            pass
                # Switch to first remaining window if any
                try:
                    remaining = driver.window_handles
                    if remaining:
                        try:
                            driver.switch_to.window(remaining[0])
                        except Exception:
                            logger.debug('Scraper: failed switching to first window handle after cleanup')
                except Exception:
                    pass
        except Exception:
            pass

        # Helper: check for common LinkedIn human verification / checkpoint pages
        def is_human_verification_page(drv):
            try:
                title = (drv.title or '').lower()
                url = (drv.current_url or '').lower()
                page_text = (drv.find_element(By.TAG_NAME, 'body').text or '').lower()
            except Exception:
                title = url = page_text = ''

            # Heuristics: LinkedIn may show 'verify', 'human', 'security check', 'are you a human', or captcha
            indicators = ['are you a human', 'security check', 'verify', 'verification', 'human verification', 'captcha', 'challenge']
            if any(ind in title for ind in indicators):
                return True
            if any(ind in url for ind in ('checkpoint', '/checkpoint/', '/m/captcha')):
                return True
            if any(ind in page_text for ind in indicators):
                return True
            return False

        # Utility to retry an action if the page updates and elements become stale.
        def retry_on_stale(fn, attempts: int = 3, delay: float = 0.4):
            last_exc = None
            for i in range(attempts):
                try:
                    return fn()
                except StaleElementReferenceException as e:
                    last_exc = e
                    time.sleep(delay)
                except ElementClickInterceptedException as e:
                    # transient overlay, wait and retry
                    last_exc = e
                    time.sleep(delay)
            # If we get here, re-raise the last exception for visibility
            if last_exc:
                raise last_exc
            return None

        # --- LinkedIn Login ---
        scraper_status['progress'] = 'Logging into LinkedIn...'
        logger.info('Scraper: Logging into LinkedIn...')
        _assert_not_stopped()
        # Perform initial login using the helper (will raise on failure)
        do_login(driver)
        # capture screenshot after login for live view
        try:
            _save_live_screenshot(driver)
        except Exception:
            pass

        # After login, detect if LinkedIn has challenged with human verification.
        # If so, pause the scraper and wait until the verification page clears, then auto-resume.
        if is_human_verification_page(driver):
            # Create a short-lived token and save artifacts so recipient can submit OTP via the UI
            token = _make_token()
            expires = datetime.utcnow() + timedelta(minutes=int(os.getenv('OTP_TOKEN_MINUTES', '15')))
            scraper_status['progress'] = 'Paused: LinkedIn requested human verification. Waiting for OTP submission.'
            scraper_status['paused_for_human_verification'] = True
            logger.warning('Scraper: Human verification detected; creating OTP token and notifying recipient/admin')

            # Save artifacts
            ss_path, html_path = _save_artifacts(driver, token)
            # Log token and artifact locations so admins can find them in logs
            try:
                logger.info(f"Scraper: Paused token={token}; screenshot={ss_path}; html={html_path}")
            except Exception:
                pass

            # Store paused session metadata (we do NOT keep the raw driver in paused_sessions to avoid pickling issues across process restarts)
            # Keep an in-memory reference to the driver so the OTP handler can inject it.
            paused_sessions[token] = {
                'created_at': datetime.utcnow().isoformat(),
                'expires_at': expires.isoformat(),
                'screenshot': ss_path,
                'html': html_path,
                'job_hint': {'url': driver.current_url},
                'driver_ref': driver
            }

            # Build OTP link for recipient - point to submit-otp endpoint
            base = os.getenv('BASE_URL') or (request.url_root.rstrip('/') if request else 'https://your-render-app')
            otp_link = f"{base}/submit-otp?token={token}"

            # Send an email to recipients (the person who will enter OTP). Use RECIPIENTS env or settings
            recipient_emails = os.getenv('RECIPIENTS') or load_settings().get('recipients') or ''
            subj = f"Action required: LinkedIn verification for scraping job - enter OTP"
            body = (
                f"We encountered a human verification while scraping LinkedIn for your request.\n\n"
                f"Please click the secure link and enter the OTP you received from LinkedIn. This link expires at {expires.isoformat()} UTC.\n\n"
                f"Open link: {otp_link}\n\n"
                "If you did not request this, ignore this message. Do not forward the link."
            )
            # Try to email recipients (best-effort). Also email ADMIN_EMAIL if configured for ops.
            try:
                _send_email_simple(recipient_emails, subj, body)
            except Exception:
                logger.exception('Failed sending OTP email to recipients')

            admin_email = os.getenv('ADMIN_EMAIL') or load_settings().get('admin_email')
            if admin_email:
                try:
                    _send_email_simple(admin_email, f"[ADMIN] Human verification paused - token {token}", f"Job paused at {driver.current_url}. Token: {token}")
                except Exception:
                    logger.exception('Failed sending admin alert email')

            # Wait for OTP submission: token will be validated by /api/submit-otp which will set scraper_status['resume_requested']=True
            wait_seconds = int(os.getenv('HUMAN_VERIFY_TIMEOUT', '900'))  # default 15 minutes
            start = time.time()
            while True:
                _assert_not_stopped()
                # If admin/recipient submitted OTP (resume_requested flag set), attempt to read from a temp storage
                if scraper_status.get('resume_requested'):
                    # The OTP handler is responsible for injecting the OTP into the page; the handler should then clear resume_requested
                    logger.info('Scraper: Resume requested flag detected; continuing')
                    scraper_status['resume_requested'] = False
                    break

                # Also auto-resume if page no longer appears to be a verification page
                try:
                    if not is_human_verification_page(driver):
                        logger.info('Scraper: Human verification appears completed; resuming')
                        break
                except Exception:
                    pass

                if time.time() - start > wait_seconds:
                    scraper_status['progress'] = 'Timeout waiting for human verification. Aborting scraper.'
                    logger.error('Scraper: Human verification timeout; aborting run')
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    scraper_status['is_running'] = False
                    return

                # Sleep small increments to be responsive to stop_event
                for _ in range(20):
                    if stop_event.is_set():
                        _assert_not_stopped()
                    time.sleep(0.1)

            # Clear paused state and continue
            scraper_status['paused_for_human_verification'] = False
            scraper_status['progress'] = 'Human verification completed; resuming scraping...'

        # --- Scrape via Keyword Search (Posts -> Sort: Latest) ---
        all_job_posts = []
        _assert_not_stopped()
        scraper_status['groups_summary'] = []
        scraper_status['last_found_total'] = 0
        scraper_status['last_sent_count'] = 0
        scraper_status['last_sent_to'] = ''

        # regex to match recent time indicators like '2h', '5 hr', '10m', 'just now', etc.
        recent_time_re = re.compile(r"\b\d+\s*(?:h|hr|m|min)\b|just now|\bjust now\b|\b\d+\s*min\b", re.IGNORECASE)

        def try_click_posts_and_sort_latest(drv, timeout: int = 15, slow: bool = False):
            wait = WebDriverWait(drv, timeout)
            logger.info('Scraper: Trying to activate Posts tab and set Sort by -> Latest%s', ' (slow mode)' if slow else '')
            # Slow mode timing helpers
            pause_short = 0.2 if not slow else 0.5
            pause_med = 0.3 if not slow else 0.8
            pause_long = 1.5 if not slow else 2.2
            # Step 1: Open content type menu and click Posts
            try:
                # Find the content-type pill (the one that shows All/People/Jobs/Posts/etc.) and open it
                ct_button_candidates = drv.find_elements(
                    By.XPATH,
                    "//button[.//span[normalize-space()='Posts'] and contains(@class,'artdeco')]|"
                    "//button[.//span[normalize-space()='All']]|//button[.//span[normalize-space()='People']]|//button[.//span[normalize-space()='Jobs']]|//button[.//span[normalize-space()='Companies']]|//button[.//span[normalize-space()='Groups']]"
                )
                if not ct_button_candidates:
                    # Fallback: any button near the action bar that contains 'Posts' text
                    ct_button_candidates = drv.find_elements(By.XPATH, "//div[contains(@class,'action-bar') or contains(@class,'search-reusables')]//button[contains(., 'Posts')]")
                if ct_button_candidates:
                    ct_btn = ct_button_candidates[0]
                    try:
                        drv.execute_script("arguments[0].scrollIntoView({block:'center'});", ct_btn)
                    except Exception:
                        pass
                    try:
                        ct_btn.click()
                    except Exception:
                        try:
                            drv.execute_script("arguments[0].click();", ct_btn)
                        except Exception:
                            pass
                    time.sleep(pause_med)
                    # Click the Posts option from the opened menu
                    try:
                        posts_opt = None
                        # Look for exact Posts label within menu/listbox
                        menus = drv.find_elements(By.XPATH, "//*[@role='menu' or @role='listbox' or contains(@class,'artdeco-dropdown')]")
                        for m in menus[:3] if menus else []:
                            opts = m.find_elements(By.XPATH, ".//span[normalize-space()='Posts']")
                            if opts:
                                posts_opt = opts[0]
                                break
                        if posts_opt is None:
                            # Global fallback
                            opts = drv.find_elements(By.XPATH, "//span[normalize-space()='Posts']")
                            if opts:
                                posts_opt = opts[0]
                        if posts_opt is not None:
                            try:
                                ActionChains(drv).move_to_element(posts_opt).pause(0.05).click(posts_opt).perform()
                            except Exception:
                                try:
                                    drv.execute_script("arguments[0].click();", posts_opt)
                                except Exception:
                                    pass
                            time.sleep(pause_med)
                        # Close menu if still open
                        try:
                            drv.switch_to.active_element.send_keys(Keys.ESCAPE)
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                logger.exception('Scraper: Failed to select Posts content type')

            # Open Sort by menu (try multiple times to handle sticky headers)
            trigger_xpath = (
                # Button near the Posts pill (case-insensitive)
                "//button[.//span[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='sort by']] | "
                "//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='sort by'] | "
                "//*[@role='button'][.//span[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='sort by'] or translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='sort by'] | "
                "//div[contains(@class,'search') or contains(@class,'action-bar') or contains(@class,'filters')]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'sort by')]"
            )
            # Small scroll up to ensure header controls are visible
            try:
                drv.execute_script('window.scrollTo(0, 0);')
                time.sleep(pause_short)
            except Exception:
                pass

            for attempt in range(4):
                triggers = drv.find_elements(By.XPATH, trigger_xpath)
                if not triggers:
                    try:
                        drv.execute_script('window.scrollBy(0, -200);')
                    except Exception:
                        pass
                    time.sleep(0.3)
                    continue
                sort_trigger = triggers[0]
                try:
                    drv.execute_script("arguments[0].scrollIntoView({block:'center'});", sort_trigger)
                except Exception:
                    pass
                try:
                    sort_trigger.click()
                except Exception:
                    try:
                        drv.execute_script("arguments[0].click();", sort_trigger)
                    except Exception:
                        pass
                # Wait for a dropdown/menu container to appear
                time.sleep(pause_short)
                # Choose 'Latest/Recent' in the menu (handles radio + 'Show results')
                latest_xpath = (
                    "//div[contains(@class,'artdeco-dropdown') or contains(@class,'overflow') or contains(@class,'artdeco') or contains(@class,'ember-view')] | "
                    "//*[@role='dialog'] | //*[@role='menu'] | //*[@role='listbox'] | //div[contains(@class,'dropdown') or contains(@class,'menu')]"
                )
                try:
                    containers = drv.find_elements(By.XPATH, latest_xpath)
                except Exception:
                    containers = []
                # Search within overlays for a label/button/span with text Latest
                clicked_latest = False
                for c in containers[:3] if containers else []:
                    try:
                        # Prefer menuitemradio/option to inspect aria-checked
                        latest_opts = c.find_elements(By.XPATH,
                            ".//*[self::span or self::*[@role='menuitemradio' or @role='option']]["
                            "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'latest') or "
                            "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'most recent')"
                            "]"
                        )
                        if latest_opts:
                            el = latest_opts[0]
                            # If the element is a span inside a menuitem, click the ancestor item instead
                            try:
                                if el.tag_name.lower() == 'span':
                                    ancestor = el.find_element(By.XPATH, "ancestor::*[@role='menuitemradio' or @role='option' or self::button][1]")
                                    if ancestor:
                                        el = ancestor
                            except Exception:
                                pass
                            # Determine if already selected via aria-checked on self or ancestor
                            selected = False
                            try:
                                if (el.get_attribute('aria-checked') or '').lower() == 'true':
                                    selected = True
                                else:
                                    ac = el.find_elements(By.XPATH, "ancestor-or-self::*[@aria-checked='true']")
                                    selected = len(ac) > 0
                            except Exception:
                                selected = False
                            if selected:
                                logger.info('Scraper: Latest appears already selected inside menu')
                                clicked_latest = True
                                break
                            try:
                                drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            except Exception:
                                pass
                            try:
                                ActionChains(drv).move_to_element(el).pause(0.05).click(el).perform()
                                clicked_latest = True
                                logger.info('Scraper: Clicked Latest option')
                                time.sleep(pause_short)
                                break
                            except Exception:
                                try:
                                    drv.execute_script("arguments[0].click();", el)
                                    clicked_latest = True
                                    logger.info('Scraper: Clicked Latest option via JS')
                                    time.sleep(pause_short)
                                    break
                                except Exception:
                                    pass
                        # If not clicked yet, try a keyboard fallback (Down, Down, Enter)
                        if not clicked_latest:
                            try:
                                active = drv.switch_to.active_element
                                active.send_keys(Keys.ARROW_DOWN)
                                time.sleep(pause_short)
                                active.send_keys(Keys.ARROW_DOWN)
                                time.sleep(pause_short)
                                active.send_keys(Keys.ENTER)
                                clicked_latest = True
                                logger.info('Scraper: Selected Latest via keyboard fallback')
                                time.sleep(pause_short)
                            except Exception:
                                pass
                    except Exception:
                        continue

                # If a "Show results" button exists, click it
                try:
                    show_btns = drv.find_elements(By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show results')]")
                    if show_btns:
                        try:
                            show_btns[0].click()
                            logger.info('Scraper: Clicked Show results')
                        except Exception:
                            drv.execute_script("arguments[0].click();", show_btns[0])
                        time.sleep(pause_short)
                except Exception:
                    pass

                # Quick visible-label verification: does the Sort by trigger show 'Latest'?
                try:
                    triggers_now = drv.find_elements(By.XPATH, trigger_xpath)
                    if triggers_now:
                        label = (triggers_now[0].text or '').strip().lower()
                        if 'latest' in label:
                            logger.info("Scraper: Sort trigger label shows 'Latest'; treating as selected")
                            try:
                                scraper_status['sort_status'] = 'recent-selected'
                            except Exception:
                                pass
                            return
                except Exception:
                    pass

                # Verify by reopening menu and inspecting aria-checked if needed
                try:
                    # Re-open menu and verify 'Recent/Latest' is selected via aria-checked
                    try:
                        sort_trigger.click()
                    except Exception:
                        drv.execute_script("arguments[0].click();", sort_trigger)
                    time.sleep(pause_med)
                    containers = drv.find_elements(By.XPATH, latest_xpath)
                    verified = False
                    for c in containers[:3] if containers else []:
                        try:
                            latest_verified = c.find_elements(By.XPATH, ".//*[@aria-checked='true' and (contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'latest') or .//span[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'latest')])]")
                            if latest_verified:
                                logger.info('Scraper: Verified Latest selected via aria-checked')
                                try:
                                    scraper_status['sort_status'] = 'recent-selected'
                                except Exception:
                                    pass
                                verified = True
                                break
                        except Exception:
                            continue
                    if verified:
                        # Close menu by pressing Escape
                        try:
                            drv.switch_to.active_element.send_keys(Keys.ESCAPE)
                        except Exception:
                            pass
                        return
                except Exception:
                    pass
                # Close any open overlays with ESC before retrying
                try:
                    drv.switch_to.active_element.send_keys(Keys.ESCAPE)
                    time.sleep(pause_short)
                except Exception:
                    pass
                # try next attempt
            logger.warning('Scraper: Failed to set Sort by -> Latest/Recent after retries; leaving current order unchanged')
            try:
                scraper_status['sort_status'] = 'recent-not-selected'
            except Exception:
                pass

        def extract_contacts_from_text(t: str):
            # Stricter email pattern to avoid false positives like 'October@9'
            email_re = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,15}\b")
            emails = email_re.findall(t or '')
            if not emails and t:
                # Tolerant collapse for 'name @ domain . com' patterns
                collapsed = re.sub(r"\s*@\s*", "@", t)
                collapsed = re.sub(r"\s*\.\s*", ".", collapsed)
                emails = email_re.findall(collapsed)
            phones = re.findall(r"\+?\d[\d\-\s().]{6,}\d", t or '')
            emails = list(dict.fromkeys(emails))
            phones = list(dict.fromkeys(phones))
            return emails, phones

        def enforce_posts_and_sort_once(drv, order: str = 'top'):
            """Make a single, gentle attempt to ensure Posts tab and (optionally) Latest are visibly selected.

            - No loops; at most one click per control to avoid reload loops.
            - If order == 'latest', try selecting Latest once; otherwise leave as Top matches.
            - Does not trigger explicit page reloads; relies on LinkedIn's own UI.
            """
            try:
                # Ensure Posts is selected (one click max)
                try:
                    posts_btn = None
                    cand = drv.find_elements(By.XPATH, "//button[contains(., 'Posts')] | //a[contains(., 'Posts')]")
                    if cand:
                        posts_btn = cand[0]
                    if posts_btn is not None:
                        drv.execute_script("arguments[0].scrollIntoView({block:'center'});", posts_btn)
                        try:
                            posts_btn.click()
                        except Exception:
                            drv.execute_script("arguments[0].click();", posts_btn)
                        time.sleep(0.3)
                except Exception:
                    pass

                if (order or '').lower() == 'latest':
                    # If a visible label already shows Latest, do nothing
                    try:
                        label_nodes = drv.find_elements(By.XPATH, "//button[contains(., 'Sort by')] | //button[@aria-haspopup='listbox']")
                        if label_nodes:
                            lab = (label_nodes[0].text or '').lower()
                            if 'latest' in lab:
                                return
                    except Exception:
                        pass
                    # Open sort menu once and choose Latest/Recent
                    try:
                        sort_btn = None
                        cands = drv.find_elements(By.XPATH, "//button[contains(., 'Sort by')] | //button[@aria-haspopup='listbox']")
                        if cands:
                            sort_btn = cands[0]
                        if sort_btn is not None:
                            try:
                                sort_btn.click()
                            except Exception:
                                drv.execute_script("arguments[0].click();", sort_btn)
                            time.sleep(0.3)
                            opt = None
                            opts = drv.find_elements(By.XPATH, "//*[(@role='option' or @role='menuitemradio') and (contains(., 'Latest') or contains(., 'Recent'))] | //li[contains(., 'Latest') or contains(., 'Recent')] | //span[contains(., 'Latest') or contains(., 'Recent')]")
                            if opts:
                                opt = opts[0]
                            if opt is not None:
                                try:
                                    opt.click()
                                except Exception:
                                    drv.execute_script("arguments[0].click();", opt)
                                time.sleep(0.3)
                            # If confirmation button appears, click once
                            try:
                                show = drv.find_elements(By.XPATH, "//button[contains(., 'Show results')]")
                                if show:
                                    try:
                                        show[0].click()
                                    except Exception:
                                        drv.execute_script("arguments[0].click();", show[0])
                                    time.sleep(0.3)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

        # Heuristic terms to reduce training/promo posts if AI misses
        promo_terms = (
            # Training/education/promotions
            'training', 'course', 'courses', 'bootcamp', 'boot camp', 'enroll', 'enrollment', 'register', 'registration',
            'demo', 'free demo', 'webinar', 'workshop', 'tutorial', 'class', 'classes', 'coaching', 'mentorship',
            'certificate', 'certification', 'mock interview', 'interview prep', 'interview preparation',
            'linkedin optimization', 'resume service', 'resume writing', 'cv writing', 'portfolio review', 'career guidance',
            'placement assistance', 'job support', 'proxy support', 'support available', 'training batch', 'new batch',
            'promo', 'promotion', 'offer', 'discount', 'sale', 'paid course', 'learn', 'learning', 'upskill', 'reskill',
            'join our training', 'guaranteed placement', 'internship training', 'fee', 'fees', 'tuition'
        )
        hiring_terms = (
            # Strong hiring/recruiting intent signals
            'hiring', 'actively hiring', 'hiring now', 'we are hiring', 'we’re hiring', 'immediate hiring',
            'opening', 'openings', 'job opening', 'job openings', 'position', 'positions', 'role', 'roles',
            'vacancy', 'vacancies', 'vacant', 'opportunity', 'opportunities', 'apply', 'apply now', 'send resume',
            'send cv', 'share resume', 'share your resume', 'resume to', 'cv to', 'email your resume', 'refer candidates',
            'looking for', 'we are looking for', 'seeking', 'need', 'required', 'requirement', 'requirements',
            'recruiting', 'recruitment', 'recruiter', 'talent acquisition',
            # Employment types and conditions
            'contract', 'c2c', 'w2', '1099', 'full-time', 'full time', 'fulltime', 'part-time', 'part time', 'parttime',
            'contract to hire', 'contract-to-hire', 'temp to perm', 'temp-to-perm', 'immediate joiners', 'start asap',
            'onsite', 'on-site', 'remote', 'remote only', 'hybrid', 'work from home'
        )
        def is_promo_training(text: str) -> bool:
            low = (text or '').lower()
            return any(p in low for p in promo_terms)
        def seems_hiring(text: str) -> bool:
            low = (text or '').lower()
            return any(h in low for h in hiring_terms)
        def is_disallowed_location(text: str) -> str | None:
            low = (text or '').lower()
            # Exclude Puerto Rico explicitly per requirement (treat as non-USA)
            if 'puerto rico' in low or '#hpepuertorico' in low or ' hpepuertorico' in low:
                return 'puerto rico'
            return None

        kw_list = []
        if keywords:
            kw_list = [k.strip() for k in str(keywords).split(',') if k.strip()]

        _flag_settings = load_settings() or {}
        ai_enabled = bool(_flag_settings.get('ai_filter_enabled', AI_FILTER_ENABLED_DEFAULT))
        try:
            scraper_status['ai_filter_enabled'] = ai_enabled
        except Exception:
            pass
        ai_stats = {'kept': 0, 'skipped': 0, 'errors': 0}

        # --- Scrape Home Feed first ---
        try:
            _assert_not_stopped()
            scraper_status['progress'] = 'Scraping LinkedIn Home Feed...'
            logger.info('Scraper: Scraping Home Feed')
            # Load feed
            driver = safe_get(driver, 'https://www.linkedin.com/feed/')
            time.sleep(3)
            # gentle scroll to load recent posts
            for _ in range(4):
                _assert_not_stopped()
                try:
                    driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
                except Exception:
                    pass
                for __ in range(15):
                    if stop_event.is_set():
                        _assert_not_stopped()
                    time.sleep(0.1)
                try:
                    _save_live_screenshot(driver)
                except Exception:
                    pass

            posts = []
            try:
                posts = driver.find_elements(By.CSS_SELECTOR, ".feed-shared-update-v2")
            except Exception:
                posts = []
            if not posts:
                try:
                    posts = driver.find_elements(By.CSS_SELECTOR, "article, .feed-shared-update-v2")
                except Exception:
                    posts = []
            logger.info(f"Scraper: Feed found {len(posts)} post elements")
            feed_count = 0
            # limit posts from feed to avoid very long runs
            try:
                max_feed = int(os.getenv('MAX_FEED_POSTS') or 50)
            except Exception:
                max_feed = 50
            for post in posts[:max_feed]:
                _assert_not_stopped()
                try:
                    # expand see more
                    more_buttons = post.find_elements(By.XPATH, ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'see more') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'more')]")
                    for b in more_buttons[:2]:
                        try:
                            retry_on_stale(lambda b=b: b.click())
                            time.sleep(0.1)
                        except Exception:
                            pass
                except Exception:
                    pass

                post_text = ''
                try:
                    post_text = post.text or ''
                except Exception:
                    post_text = ''
                if not post_text:
                    continue
                if not bool(recent_time_re.search(post_text)):
                    continue

                # AI filter
                reason = ''
                if ai_enabled:
                    try:
                        keep, reason = ai_is_usa_hiring_post(post_text)
                        if keep and is_promo_training(post_text) and not seems_hiring(post_text):
                            keep = False
                            reason = (reason or '') + ' | promo-training'
                        if keep:
                            bad_loc = is_disallowed_location(post_text)
                            if bad_loc:
                                keep = False
                                reason = (reason or '') + f' | non-usa-location: {bad_loc}'
                        if not keep:
                            ai_stats['skipped'] += 1
                            continue
                        else:
                            ai_stats['kept'] += 1
                    except Exception:
                        ai_stats['errors'] += 1
                        continue

                emails, phones = extract_contacts_from_text(post_text)
                try:
                    anchors = post.find_elements(By.TAG_NAME, 'a')
                    stable_id = _extract_linkedin_activity_id_from_anchors(anchors)
                    for a in anchors:
                        try:
                            href = (a.get_attribute('href') or '')
                            if href.startswith('mailto:'):
                                addr = href.split(':', 1)[1].split('?')[0]
                                if addr and addr not in emails:
                                    emails.append(addr)
                        except Exception:
                            pass
                except Exception:
                    anchors = []
                    stable_id = None

                if not stable_id:
                    stable_id = f"feed:{hashlib.sha256(_normalize_text_for_id(post_text).encode('utf-8')).hexdigest()}"

                all_job_posts.append({
                    'text': post_text,
                    'raw_text': post_text,
                    'emails': emails,
                    'phones': phones,
                    'group_name': 'Home Feed',
                    'group_url': 'https://www.linkedin.com/feed/',
                    'id': stable_id,
                    'ai_reason': reason
                })
                feed_count += 1

            try:
                gs = scraper_status.get('groups_summary', [])
                gs.append({'name': 'Home Feed', 'url': 'https://www.linkedin.com/feed/', 'recent_count': feed_count})
                scraper_status['groups_summary'] = gs
                scraper_status['last_found_total'] = len(all_job_posts)
            except Exception:
                pass
        except Exception:
            logger.exception('Scraper: Error while scraping Home Feed')

        if use_keywords_search and kw_list:
            for kw in kw_list:
                _assert_not_stopped()
                # Posts + Latest using LinkedIn's date_posted sort param (encoded quotes) + origin
                search_url = (
                    f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(kw)}"
                    f"&origin=FACETED_SEARCH&sortBy=%22date_posted%22"
                )
                scraper_status['progress'] = f"Searching posts for: {kw} (Latest by date posted)..."
                logger.info(scraper_status['progress'])
                try:
                    _assert_not_stopped()
                    driver = safe_get(driver, search_url)
                    time.sleep(3)
                    # One-shot Sort by → Latest (visual confirmation; no loops)
                    try:
                        enforce_posts_and_sort_once(driver, order='latest')
                    except Exception:
                        pass

                    # Scroll to load more
                    for _ in range(3):
                        _assert_not_stopped()
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        # Stop-aware sleep
                        for __ in range(15):
                            if stop_event.is_set():
                                _assert_not_stopped()
                            time.sleep(0.1)
                        # Update live screenshot after scrolling
                        try:
                            _save_live_screenshot(driver)
                        except Exception:
                            pass

                    # Collect post containers (try multiple selectors)
                    posts = []
                    try:
                        posts = driver.find_elements(By.CSS_SELECTOR, ".reusable-search__result-container")
                    except Exception:
                        posts = []
                    if not posts:
                        try:
                            posts = driver.find_elements(By.CSS_SELECTOR, "article, .feed-shared-update-v2")
                        except Exception:
                            posts = []
                    logger.info(f"Scraper: Search '{kw}' found {len(posts)} result elements")

                    recent_count = 0
                    for post in posts:
                        _assert_not_stopped()
                        # Expand 'See more' to capture full text where possible
                        try:
                            more_buttons = post.find_elements(By.XPATH, ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'see more') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'more')]")
                            for b in more_buttons[:2]:
                                try:
                                    retry_on_stale(lambda b=b: b.click())
                                    time.sleep(0.1)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        text = ''
                        try:
                            text = (post.text or '').strip()
                        except Exception:
                            text = ''
                        if not text:
                            continue
                        if not bool(recent_time_re.search(text)):
                            continue

                        # AI filter: keep only USA hiring posts when enabled
                        reason = ''
                        if ai_enabled:
                            keep, reason = ai_is_usa_hiring_post(text)
                            # Extra guard: filter out obvious training/promo if not clearly hiring
                            if keep and is_promo_training(text) and not seems_hiring(text):
                                keep = False
                                reason = (reason or '') + ' | promo-training'
                            # Disallow specific non-USA locations
                            if keep:
                                bad_loc = is_disallowed_location(text)
                                if bad_loc:
                                    keep = False
                                    reason = (reason or '') + f' | non-usa-location: {bad_loc}'
                            if not keep:
                                ai_stats['skipped'] += 1
                                continue
                            else:
                                ai_stats['kept'] += 1
                        else:
                            # Without AI: drop promo/training if not hiring-like
                            if is_promo_training(text) and not seems_hiring(text):
                                continue
                            bad_loc = is_disallowed_location(text)
                            if bad_loc:
                                continue

                        emails, phones = extract_contacts_from_text(text)
                        # Scan anchors for mailto:/tel: (search results often use anchors)
                        try:
                            anchors = post.find_elements(By.TAG_NAME, 'a')
                            # Try to extract a stable LinkedIn id
                            stable_id = _extract_linkedin_activity_id_from_anchors(anchors)
                            for a in anchors:
                                try:
                                    href = (a.get_attribute('href') or '')
                                    if href.startswith('mailto:'):
                                        addr = href.split(':', 1)[1].split('?')[0]
                                        if addr and addr not in emails:
                                            emails.append(addr)
                                    if href.startswith('tel:'):
                                        tel = href.split(':', 1)[1]
                                        if tel and tel not in phones:
                                            phones.append(tel)
                                except Exception:
                                    pass
                        except Exception:
                            anchors = []
                            stable_id = None
                        # Build a stable id from activity if available; else normalized text
                        if not stable_id:
                            stable_id = f"txt:{hashlib.sha256((_normalize_text_for_id(text) + '|kw:' + kw).encode('utf-8')).hexdigest()}"
                        jid = stable_id
                        all_job_posts.append({
                            'text': text,
                            'raw_text': text,
                            'emails': emails,
                            'phones': phones,
                            'group_name': f"Search: {kw}",
                            'group_url': search_url,
                            'id': jid,
                            'ai_reason': reason
                        })
                        recent_count += 1

                    # status summary line for UI
                    try:
                        gs = scraper_status.get('groups_summary', [])
                        gs.append({'name': f"Search: {kw}", 'url': search_url, 'recent_count': recent_count})
                        scraper_status['groups_summary'] = gs
                        scraper_status['last_found_total'] = len(all_job_posts)
                    except Exception:
                        pass
                except Exception:
                    logger.exception(f"Scraper: Error during keyword search for '{kw}'")

        # --- Scrape Groups ---
        target_groups = groups if groups else []

        for group in target_groups:
            _assert_not_stopped()
            scraper_status['progress'] = f"Scraping group: {group['name']}..."
            logger.info(f"Scraper: Scraping group: {group['name']}...")
            driver = safe_get(driver, group['url'])
            time.sleep(4)

            # Scroll to load more posts
            for _ in range(3):
                _assert_not_stopped()
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                # Stop-aware sleep
                for __ in range(20):
                    if stop_event.is_set():
                        _assert_not_stopped()
                    time.sleep(0.1)
                # Update live screenshot after scrolling
                try:
                    _save_live_screenshot(driver)
                except Exception:
                    pass

            posts = driver.find_elements(By.CSS_SELECTOR, ".feed-shared-update-v2")
            logger.info(f"Scraper: Found {len(posts)} raw post elements in group '{group['name']}'")
            recent_count = 0
            sample_texts = []
            for post in posts:
                _assert_not_stopped()
                # Try to expand the post if there's a 'See more' button/link so we get full text
                try:
                    see_more_buttons = post.find_elements(By.XPATH, ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'see more')]")
                    for b in see_more_buttons:
                        try:
                            retry_on_stale(lambda b=b: b.click())
                            time.sleep(0.2)
                        except Exception:
                            pass
                except Exception:
                    pass

                post_text = post.text.strip()
                if not post_text:
                    continue

                # Clean the post: remove UI artifacts like Likes, Comments, Shares and profile snippets
                lines = [l.strip() for l in post_text.splitlines() if l.strip()]
                cleaned_lines = []
                prev = None
                in_comment_section = False
                for l in lines:
                    low = l.lower()
                    # Skip feed UI numbering like 'Feed post number 24'
                    if low.startswith('feed post number'):
                        continue
                    # If we reach the comments header like '1 comment' or '2 comments', stop including further content
                    if not in_comment_section:
                        if low == '1 comment' or bool(re.match(r"^\d+\s+comments?$", low)):
                            in_comment_section = True
                            continue
                    if in_comment_section:
                        # Drop all subsequent lines from comments area
                        continue
                    # Skip social interaction lines
                    if low.startswith('like') or low.startswith('likes') or low.startswith('comment') or low.startswith('comments') or 'share' in low:
                        continue
                    # Skip follow/connect/profile action lines
                    if low.startswith('follow') or low.startswith('connect') or low.startswith('followed') or low.startswith('follow us'):
                        continue
                    # Remove premium indicators
                    if 'premium' in low:
                        continue
                    # Skip small profile/headline fragments (heuristic)
                    if len(l.split()) <= 3 and ('pronoun' in low or 'headline' in low or low.endswith('•')):
                        continue
                    # Skip degree/grade lines like '3rd', '2nd', '1st', '3rd+' etc if very short
                    if re.search(r"\b\d+(st|nd|rd|th)\b", low) and len(l.split()) <= 4:
                        continue
                    # Skip common degree keywords
                    if any(x in low for x in ('b.sc', 'bsc', 'm.sc', 'msc', 'bachelor', 'master', 'degree', 'mba', 'phd')):
                        continue
                    # Collapse consecutive duplicate lines (e.g., poster name repeated)
                    if prev is not None and l == prev:
                        continue
                    cleaned_lines.append(l)
                    prev = l
                cleaned_text = '\n'.join(cleaned_lines).strip()

                # Determine whether this post looks recent using regex
                is_recent = bool(recent_time_re.search(post_text))
                if not is_recent:
                    continue

                # Keyword logic: keywords passed in (string) or None
                kw_list = []
                if keywords:
                    kw_list = [k.strip().lower() for k in keywords.split(',') if k.strip()]

                matches_keyword = False
                if kw_list:
                    for kw in kw_list:
                        if kw in post_text.lower() or kw in cleaned_text.lower():
                            matches_keyword = True
                            break

                # AI filter: keep only USA hiring posts when enabled
                reason = ''
                if ai_enabled:
                    keep, reason = ai_is_usa_hiring_post(post_text)
                    # Extra guard: filter out obvious training/promo if not clearly hiring
                    try:
                        if keep and is_promo_training(post_text) and not seems_hiring(post_text):
                            keep = False
                            reason = (reason or '') + ' | promo-training'
                    except Exception:
                        pass
                    # Disallow specific non-USA locations
                    try:
                        if keep:
                            bad_loc = is_disallowed_location(post_text)
                            if bad_loc:
                                keep = False
                                reason = (reason or '') + f' | non-usa-location: {bad_loc}'
                    except Exception:
                        pass
                    if not keep:
                        ai_stats['skipped'] += 1
                        continue
                    else:
                        ai_stats['kept'] += 1
                else:
                    # Without AI: drop promo/training if not hiring-like
                    try:
                        if is_promo_training(post_text) and not seems_hiring(post_text):
                            continue
                        bad_loc = is_disallowed_location(post_text)
                        if bad_loc:
                            continue
                    except Exception:
                        pass

                # Extract emails and phone numbers (robust). Also check for mailto:/tel: anchors inside the post DOM
                def extract_contacts_from_text(t: str):
                    # Stricter email regex to avoid false positives like 'October@9'
                    email_re = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,15}\b")
                    txt = t or ''
                    emails = email_re.findall(txt)
                    # Try tolerant collapse and deobfuscation (at/dot)
                    if not emails and txt:
                        collapsed = re.sub(r"\s*@\s*", "@", txt)
                        collapsed = re.sub(r"\s*\.\s*", ".", collapsed)
                        emails = email_re.findall(collapsed)
                    if not emails and txt:
                        deob = re.sub(r"\bat\b", "@", txt, flags=re.IGNORECASE)
                        deob = re.sub(r"\bdot\b", ".", deob, flags=re.IGNORECASE)
                        emails = email_re.findall(deob)
                    phones = re.findall(r"\+?\d[\d\-\s().]{6,}\d", txt)
                    # normalize unique
                    emails = list(dict.fromkeys(emails))
                    phones = list(dict.fromkeys(phones))
                    return emails, phones

                # Prefer contacts from cleaned (no-comments) text; fallback to raw only if none
                emails, phones = extract_contacts_from_text(cleaned_text)
                if not emails and not phones:
                    emails2, phones2 = extract_contacts_from_text(post_text)
                    if emails2:
                        emails = emails2
                    if phones2:
                        phones = phones2

                # Check for mailto: and tel: links; avoid pulling from comment areas
                try:
                    anchors = post.find_elements(By.TAG_NAME, 'a')
                    stable_id = _extract_linkedin_activity_id_from_anchors(anchors)
                    # Only use anchor-based contacts if we didn't already extract from text
                    if (not emails) and (not phones):
                        contact_anchors = post.find_elements(
                            By.XPATH,
                            ".//a[starts-with(@href,'mailto:') or starts-with(@href,'tel:')][not(ancestor::*[contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'comment')])]"
                        )
                        for a in contact_anchors:
                            try:
                                href = (a.get_attribute('href') or '')
                                if href.startswith('mailto:'):
                                    addr = href.split(':', 1)[1].split('?')[0]
                                    if addr and addr not in emails:
                                        emails.append(addr)
                                if href.startswith('tel:'):
                                    tel = href.split(':', 1)[1]
                                    if tel and tel not in phones:
                                        phones.append(tel)
                            except Exception:
                                pass
                except Exception:
                    anchors = []
                    stable_id = None

                recent_count += 1
                # compute a stable id: prefer LinkedIn activity/post path; else normalized text
                if not stable_id:
                    stable_id = f"txt:{hashlib.sha256(_normalize_text_for_id(post_text).encode('utf-8')).hexdigest()}"
                hash_id = stable_id
                job = {
                    'text': cleaned_text or post_text,
                    'raw_text': post_text,
                    'emails': emails,
                    'phones': phones,
                    'group_name': group['name'],
                    'group_url': group['url'],
                    'id': hash_id,
                    'ai_reason': reason
                }
                all_job_posts.append(job)
                if emails:
                    logger.info(f"Scraper: Found emails in post: {emails}")
                if len(sample_texts) < 3:
                    sample_texts.append((cleaned_text or post_text)[:300])

            logger.info(f"Scraper: For group '{group['name']}' recent posts: {recent_count}")
            if sample_texts:
                logger.info(f"Scraper: Sample recent post(s) from '{group['name']}': {sample_texts}")
            # update a concise group-level summary that the UI can read
            try:
                # append or update groups_summary for UI
                gs = scraper_status.get('groups_summary', [])
                gs.append({'name': group['name'], 'url': group['url'], 'recent_count': recent_count})
                scraper_status['groups_summary'] = gs
                # also update a running total for immediate UI feedback
                scraper_status['last_found_total'] = len(all_job_posts)
            except Exception:
                logger.exception('Scraper: failed to update groups_summary status')

        # Summarize AI filtering and proceed to filter out already-sent jobs
        scraper_status['ai_filter_stats'] = ai_stats
        scraper_status['progress'] = f'Found {len(all_job_posts)} potential jobs after AI filter. Filtering for new ones...'
        logger.info(scraper_status['progress'])

        # --- Filter New Jobs ---
        try:
            # Union DB ids with file-based ids (fallback) so a transient DB error doesn't cause duplicates
            sent_job_ids = set(get_sent_job_ids() or set())
        except Exception:
            # fallback to file-based approach if DB fails
            logger.exception('Scraper: Failed to read sent job ids from DB; falling back to JSON file')
            sent_job_ids = set()
        # Also read JSON file to add to the set (regardless) for extra safety
        try:
            if os.path.exists(SENT_JOBS_FILE):
                with open(SENT_JOBS_FILE, 'r', encoding='utf-8') as f:
                    sent_jobs = json.load(f) or []
                for job in sent_jobs:
                    jid = (job.get('id') or '').strip()
                    if jid:
                        sent_job_ids.add(jid)
        except Exception:
            logger.exception('Scraper: Ignoring errors reading file-based sent jobs while unioning ids')

        new_jobs = [job for job in all_job_posts if job['id'] not in sent_job_ids]

        # Compute unique extracted emails from new_jobs so the UI can show a live count
        try:
            unique_emails = set()
            for job in new_jobs:
                try:
                    emails = job.get('emails') or []
                    if not emails:
                        # fallback to extracting from text
                        try:
                            emails, _ = extract_contacts_from_text(job.get('text') or job.get('raw_text') or '')
                        except Exception:
                            emails = []
                    for e in emails:
                        k = (e or '').strip().lower()
                        if k:
                            unique_emails.add(k)
                except Exception:
                    continue
            try:
                scraper_status['extracted_emails_count'] = len(unique_emails)
                scraper_status['extracted_emails_sample'] = list(unique_emails)[:5]
            except Exception:
                pass
        except Exception:
            logger.exception('Failed computing extracted emails count for status')

        # If running in "hold emails only" mode, collect and persist extracted emails and skip sending.
        if hold_emails_only:
            try:
                scraper_status['progress'] = f'Hold mode: collecting emails from {len(new_jobs)} new job(s)...'
                logger.info(scraper_status['progress'])
                unique = {}
                for job in new_jobs:
                    # prefer job['emails'] (list); if empty, try extracting from text
                    emails = job.get('emails') or []
                    if not emails:
                        try:
                            emails, _ = extract_contacts_from_text(job.get('text') or job.get('raw_text') or '')
                        except Exception:
                            emails = []
                    for e in emails:
                        key = (e or '').strip().lower()
                        if not key:
                            continue
                        if key not in unique:
                            unique[key] = {
                                'email': key,
                                'job_id': job.get('id'),
                                'group_name': job.get('group_name'),
                                'group_url': job.get('group_url'),
                                'snippet': (job.get('text') or '')[:500]
                            }
                extracted_list = list(unique.values())
                try:
                    write_json_atomic(EXTRACTED_EMAILS_FILE, extracted_list)
                    scraper_status['progress'] = f'Hold mode: saved {len(extracted_list)} unique email(s) to {EXTRACTED_EMAILS_FILE}'
                    scraper_status['extracted_emails_count'] = len(extracted_list)
                    scraper_status['extracted_emails_file'] = EXTRACTED_EMAILS_FILE
                    logger.info(scraper_status['progress'])
                except Exception:
                    logger.exception('Failed to write extracted emails to file')
                # Do not attempt to send emails in hold mode; exit the scraper task.
            except Exception:
                logger.exception('Error while collecting emails in hold mode')
            finally:
                if 'driver' in locals():
                    try:
                        driver.quit()
                    except Exception:
                        pass
                scraper_status['is_running'] = False
                return

        # --- Send Emails ---
        # We'll track which jobs were actually sent and only save those.
        sent_jobs_local = []
        if new_jobs:
            scraper_status['progress'] = f'Sending {len(new_jobs)} new job emails...'
            logger.info(scraper_status['progress'])
            try:
                # Build a sender pool: either from settings['senders'] or fallback to single env creds
                settings_local = load_settings() or {}
                senders = []
                try:
                    configured = settings_local.get('senders')
                    if configured and isinstance(configured, list) and configured:
                        for s in configured:
                            # Expect structure: {'user':..., 'pass':..., 'host': 'smtp.example.com', 'port': 465, 'use_ssl': True}
                            user = s.get('user') or s.get('email')
                            pwd = s.get('pass') or s.get('password')
                            host = s.get('host') or os.getenv('SMTP_HOST') or 'smtp.gmail.com'
                            port = int(s.get('port') or os.getenv('SMTP_PORT') or (465))
                            use_ssl = bool(s.get('use_ssl') if 'use_ssl' in s else True)
                            if user and pwd:
                                senders.append({'user': user, 'pass': pwd, 'host': host, 'port': port, 'use_ssl': use_ssl})
                except Exception:
                    logger.exception('Scraper: Failed to parse configured senders; falling back to env')

                if not senders:
                    # fallback to single configured env credentials for backward-compat
                    if gmail_user and gmail_pass:
                        senders = [{'user': gmail_user, 'pass': gmail_pass, 'host': os.getenv('SMTP_HOST') or 'smtp.gmail.com', 'port': int(os.getenv('SMTP_PORT') or 465), 'use_ssl': True}]
                    else:
                        raise RuntimeError('No sender credentials configured (settings.senders or GMAIL_USER/GMAIL_PASS)')

                # Ensure on-disk sent jobs list is available for compatibility (not used for uniqueness now)
                try:
                    if os.path.exists(SENT_JOBS_FILE):
                        with open(SENT_JOBS_FILE, 'r', encoding='utf-8') as f:
                            existing = json.load(f)
                    else:
                        existing = []
                except json.JSONDecodeError:
                    logger.warning(f"Scraper: {SENT_JOBS_FILE} is empty or invalid JSON; starting with empty list")
                    existing = []
                except Exception:
                    logger.exception(f"Scraper: Unexpected error reading {SENT_JOBS_FILE}; starting with empty list")
                    existing = []

                # Rotate senders across messages (round-robin)
                for i, job in enumerate(new_jobs):
                    _assert_not_stopped()
                    scraper_status['progress'] = f'Sending email {i+1}/{len(new_jobs)}...'
                    logger.info(scraper_status['progress'])
                    sender = senders[i % len(senders)]

                    # Compose message (include raw text and AI reason when present)
                    short_id = job['id'][:8]
                    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
                    # Try to extract a role/title for a clearer subject
                    role = extract_role_from_text(job.get('raw_text') or job.get('text') or '')
                    if role:
                        subject = f"New job found: \"{role}\" — found in \"{job['group_name']}\" [{short_id}-{ts}]"
                    else:
                        subject = f"New LinkedIn Job Lead: From '{job['group_name']}' [{short_id}-{ts}]"
                    contacts = ''
                    if job.get('emails'):
                        contacts += 'Emails: ' + ', '.join(job.get('emails')) + '\n'
                    if job.get('phones'):
                        contacts += 'Phones: ' + ', '.join(job.get('phones')) + '\n'
                    # Compose message: include cleaned content and always attach the full raw post so the recipient
                    # can read and decide. Also set Reply-To to the first extracted contact email when available.
                    content = job.get('text') or ''
                    raw = job.get('raw_text') or ''
                    # Always append the raw post after a separator so recipient has full context
                    if raw and raw.strip() and raw.strip() != (content or '').strip():
                        full_content = f"{content}\n\n----- Full Raw Post -----\n{raw}"
                    else:
                        full_content = content
                    if job.get('ai_reason'):
                        full_content = f"{full_content}\n\n(AI reason: {job.get('ai_reason')})"
                    body = (
                        f"A new potential job opportunity was found.\n\n"
                        f"Group: {job['group_name']}\nGroup URL: {job['group_url']}\n"
                        f"------------------------------------\n\n{full_content}\n\n{contacts}"
                    )
                    msg = MIMEText(body)
                    msg['Subject'] = subject
                    msg['From'] = sender['user']
                    # If an extracted contact exists, set Reply-To so recipient's Reply will go to that contact
                    reply_to = None
                    try:
                        extracted_contacts = (job.get('emails') or [])
                        if extracted_contacts:
                            reply_to = extracted_contacts[0]
                    except Exception:
                        reply_to = None
                    if reply_to:
                        try:
                            msg['Reply-To'] = reply_to
                        except Exception:
                            pass
                    msg['To'] = recipient_emails
                    # Add a stable X-Job-ID header so recipients can correlate messages
                    try:
                        msg_id = f"<{uuid.uuid4()}@linkedin-scraper>"
                        msg['Message-ID'] = msg_id
                        msg['X-Job-ID'] = job['id']
                    except Exception:
                        logger.exception('Scraper: Failed to set message headers')

                    # Send using the chosen sender credentials (login per-send for simplicity)
                    try:
                        # Sanitize recipient list and log attempt (masking sensitive parts)
                        recipients_raw = recipient_emails or ''
                        recipient_list = [r.strip() for r in (recipients_raw.split(',') if recipients_raw else []) if r.strip()]
                        logger.info(f"Scraper: Attempting send: sender={sender.get('user')} to recipients={recipient_list}")
                        if not recipient_list:
                            logger.warning('Scraper: No recipients configured; skipping send for job id %s', job.get('id'))
                        else:
                            if sender.get('use_ssl'):
                                server = smtplib.SMTP_SSL(sender.get('host'), int(sender.get('port')))
                            else:
                                server = smtplib.SMTP(sender.get('host'), int(sender.get('port')))
                                server.starttls()
                            try:
                                server.login(sender.get('user'), sender.get('pass'))
                                server.sendmail(sender.get('user'), recipient_list, msg.as_string())
                                server.quit()
                            except smtplib.SMTPAuthenticationError as e:
                                logger.exception('SMTP auth failed for sender %s', sender.get('user'))
                                try:
                                    scraper_status['last_smtp_error'] = f'auth:{str(e)}'
                                except Exception:
                                    pass
                                raise
                            except Exception as e:
                                logger.exception('SMTP send failed for sender %s', sender.get('user'))
                                try:
                                    scraper_status['last_smtp_error'] = f'send:{str(e)}'
                                except Exception:
                                    pass
                                raise

                        # Persist this job to DB immediately so it won't be re-sent
                        try:
                            if add_sent_job(job):
                                logger.info(f"Scraper: Persisted sent job id {job['id'][:60]} to DB immediately")
                            else:
                                logger.info(f"Scraper: Sent job id {job['id'][:60]} already exists or failed to persist")
                        except Exception:
                            logger.exception('Scraper: Failed to persist sent job to DB immediately')

                        sent_jobs_local.append(job)
                        scraper_status['last_sent_count'] = len(sent_jobs_local)
                        scraper_status['last_sent_to'] = recipient_emails
                        logger.info(f"Scraper: Email sent for job id {job['id'][:60]} using sender {sender.get('user')}")
                    except Exception:
                        logger.exception(f"Scraper: Failed to send email for job id {job['id'][:60]} using sender {sender.get('user')}")
                    # Stop-aware delay between emails
                    waited = 0.0
                    step = 0.1
                    total = max(0.0, float(delay_seconds))
                    while waited < total:
                        if stop_event.is_set():
                            _assert_not_stopped()
                        time.sleep(step)
                        waited += step

                scraper_status['progress'] = f'Successfully sent {len(sent_jobs_local)} emails.'
                logger.info(scraper_status['progress'])
            except Exception as e:
                # If login fails or other sending error occurs, avoid saving any jobs so they will be retried next run
                scraper_status['progress'] = f'Email Error: {str(e)}. Make sure your SMTP credentials (or App Password) are correct.'
                logger.exception('Scraper: Email sending/login error')
                sent_jobs_local = []
        else:
            scraper_status['progress'] = 'No new jobs found this time.'
            logger.info(scraper_status['progress'])

        # --- Save Sent Jobs ---
        # Ensure any sent_jobs_local are recorded in the DB. If DB fails, fall back to file append.
        try:
            for job in sent_jobs_local:
                try:
                    add_sent_job(job)
                except Exception:
                    logger.exception('Scraper: Failed to add sent job to DB; will try file fallback')

            # If DB is unavailable and we still have local sent jobs, try file fallback
            if not sent_jobs_local:
                logger.info('Scraper: No new sent jobs to save')
            else:
                logger.info(f"Scraper: Recorded {len(sent_jobs_local)} sent job(s) to DB (or attempted)")
        except Exception:
            logger.exception('Scraper: Error saving sent jobs to DB; falling back to JSON file')
            try:
                if os.path.exists(SENT_JOBS_FILE):
                    try:
                        with open(SENT_JOBS_FILE, 'r') as f:
                            existing = json.load(f)
                    except Exception:
                        existing = []
                else:
                    existing = []

                existing_ids = set(job.get('id') for job in existing)
                to_add = [job for job in sent_jobs_local if job['id'] not in existing_ids]
                if to_add:
                    existing.extend(to_add)
                    with open(SENT_JOBS_FILE, 'w') as f:
                        json.dump(existing, f, indent=2)
                    logger.info(f"Scraper: Saved {len(to_add)} newly sent job(s) to {SENT_JOBS_FILE}")
                else:
                    logger.info('Scraper: No new sent jobs to save (file fallback)')
            except Exception:
                logger.exception('Scraper: Final fallback failed when saving sent jobs to file')

    except StopRequested:
        scraper_status['progress'] = 'Stopping now — user requested stop.'
        logger.info('Scraper: Stop requested by user; shutting down...')
    except Exception as e:
        scraper_status['progress'] = f'An error occurred: {str(e)}'
        logger.exception('Scraper: Unhandled exception')
    finally:
        if 'driver' in locals():
            try:
                driver.quit()
            except Exception:
                logger.warning('Scraper: Error quitting driver')
        scraper_status['is_running'] = False
        # The task is done, but we leave the final message for the user to see.


# --- FLASK ROUTES ---
@app.route('/', methods=['GET', 'POST'])
def index():
    global scraper_status
    if request.method == 'POST':
        if scraper_status['is_running']:
            return "A scraper task is already running. Please wait for it to complete.", 400
        # Get form data (sender creds come from environment/.env and are hidden from the UI)
        # IMPORTANT: Use a Google App Password stored in .env as GMAIL_PASS
        gmail_user = os.getenv('GMAIL_USER')
        gmail_pass = os.getenv('GMAIL_PASS')
        recipient_emails = request.form.get('recipient_emails') or os.getenv('RECIPIENTS')
        linkedin_user = request.form.get('linkedin_user') or os.getenv('LINKEDIN_USER')
        linkedin_pass = request.form.get('linkedin_pass') or os.getenv('LINKEDIN_PASS')

        # Persist recipients and LinkedIn creds so user doesn't have to re-enter them
        settings = load_settings()
        changed = False
        # Optional settings: delay (seconds) and whether to send each post separately
        try:
            delay_seconds = int(request.form.get('delay_seconds') or os.getenv('DELAY_SECONDS') or 10)
        except ValueError:
            delay_seconds = 10
        send_separately = request.form.get('send_separately') == 'on' or os.getenv('SEND_SEPARATELY', 'true').lower() in ('1','true','yes')

        if recipient_emails:
            settings['recipients'] = recipient_emails
            changed = True
        if linkedin_user:
            settings['linkedin_user'] = linkedin_user
            changed = True
        if linkedin_pass:
            settings['linkedin_pass'] = linkedin_pass
            changed = True
        if changed:
            save_settings(settings)

        # Keywords options (optional)
        # Note: checkboxes are absent when unchecked, so we interpret absence as False
        keywords = (request.form.get('keywords') or '').strip()
        require_keywords = (request.form.get('require_keywords') == 'on')
        use_keywords_search = (request.form.get('use_keywords_search') == 'on')
        # Hold-only extraction mode: when checked, scraper will only collect emails and save them to a file
        hold_emails_only = (request.form.get('hold_emails_only') == 'on')
        search_sort_order = (request.form.get('search_sort_order') or 'top').strip().lower()
        if search_sort_order not in ('top','latest'):
            search_sort_order = 'top'
        ai_filter_enabled = (request.form.get('ai_filter_enabled') == 'on')
        include_raw_post = (request.form.get('include_raw_post') == 'on')
        # persist keyword and toggles exactly as provided (empty keyword clears saved value)
        settings['keywords'] = keywords
        settings['require_keywords'] = bool(require_keywords)
        settings['use_keywords_search'] = bool(use_keywords_search)
        settings['ai_filter_enabled'] = bool(ai_filter_enabled)
        settings['include_raw_post'] = bool(include_raw_post)
        settings['search_sort_order'] = search_sort_order
        save_settings(settings)
        try:
            scraper_status['ai_filter_enabled'] = bool(ai_filter_enabled)
        except Exception:
            pass

        # Validate sender credentials are present in environment (we don't accept them from UI)
        if not gmail_user or not gmail_pass:
            alert = (
                'Sender credentials are not configured. Please set GMAIL_USER and GMAIL_PASS in your .env file and restart the app.'
            )
            return render_template('index.html', settings=settings, env={'GMAIL_USER': gmail_user, 'RECIPIENTS': recipient_emails, 'LINKEDIN_USER': linkedin_user}, alert=alert, admin_token=ADMIN_TOKEN)
        # Load saved groups from settings (if any)
        settings = load_settings()
        groups = settings.get('groups') or []

        # Start the scraper in a new thread
        scraper_thread = threading.Thread(
            target=scraper_task,
            args=(gmail_user, gmail_pass, recipient_emails, linkedin_user, linkedin_pass, delay_seconds, send_separately, groups, keywords, require_keywords, use_keywords_search, hold_emails_only),
            daemon=True
        )
        scraper_thread.start()

        # Return the control panel page so the frontend JS remains active and will show progress.
        # Passing a small flag tells the template to show a "started" alert.
        settings = load_settings()
        env = {
            'GMAIL_USER': os.getenv('GMAIL_USER'),
            'RECIPIENTS': recipient_emails,
            'LINKEDIN_USER': linkedin_user,
            'WEBSOCKIFY_URL': os.getenv('WEBSOCKIFY_URL') or ''
        }
        return render_template('index.html', started=True, settings=settings, env=env, admin_token=ADMIN_TOKEN)

    # For GET requests render the control panel, prefill values from settings and .env
    settings = load_settings()
    # initialize ai setting in scraper_status so UI shows it on first load
    try:
        scraper_status['ai_filter_enabled'] = bool(settings.get('ai_filter_enabled', AI_FILTER_ENABLED_DEFAULT))
    except Exception:
        pass
    env = {
        'GMAIL_USER': os.getenv('GMAIL_USER'),
        'RECIPIENTS': os.getenv('RECIPIENTS'),
        'LINKEDIN_USER': os.getenv('LINKEDIN_USER'),
        'WEBSOCKIFY_URL': os.getenv('WEBSOCKIFY_URL') or ''
    }
    # Pass ADMIN_TOKEN to the template so client JS can include it in admin requests if present
    return render_template('index.html', settings=settings, env=env, admin_token=ADMIN_TOKEN)


@app.route('/groups', methods=['POST'])
def add_group():
    data = request.json
    url = data.get('url')
    name = data.get('name') or url
    if not url:
        return jsonify({'error': 'url required'}), 400
    settings = load_settings()
    groups = settings.get('groups', [])
    groups.append({'url': url, 'name': name})
    settings['groups'] = groups
    save_settings(settings)
    return jsonify({'ok': True, 'groups': groups})


@app.route('/groups', methods=['GET'])
def list_groups():
    settings = load_settings()
    return jsonify({'groups': settings.get('groups', [])})


@app.route('/groups/<int:index>', methods=['DELETE'])
def delete_group(index):
    settings = load_settings()
    groups = settings.get('groups', [])
    if 0 <= index < len(groups):
        groups.pop(index)
        settings['groups'] = groups
        save_settings(settings)
        return jsonify({'ok': True, 'groups': groups})
    return jsonify({'error': 'index out of range'}), 400

@app.route('/groups', methods=['DELETE'])
def delete_group_by_url():
    """Delete a group by URL passed as query param (?url=...) or JSON body {url: ...}."""
    url = request.args.get('url')
    if not url:
        try:
            body = request.get_json(silent=True) or {}
            url = body.get('url')
        except Exception:
            url = None
    if not url:
        return jsonify({'error': 'url required'}), 400
    settings = load_settings()
    groups = settings.get('groups', [])
    new_groups = [g for g in groups if (g.get('url') or '') != url]
    if len(new_groups) == len(groups):
        return jsonify({'ok': False, 'error': 'group url not found', 'groups': groups}), 404
    settings['groups'] = new_groups
    save_settings(settings)
    return jsonify({'ok': True, 'groups': new_groups})

@app.route('/status')
def status():
    """An endpoint to check the scraper's status from the frontend."""
    # Report stop capability and whether a stop is pending
    report = dict(scraper_status)
    try:
        report['stop_requested'] = stop_event.is_set()
    except Exception:
        report['stop_requested'] = False
    # If there's an active paused session, include a public token and a submit URL
    try:
        if paused_sessions:
            # pick the most-recent paused token (insertion order)
            try:
                token = next(iter(paused_sessions.keys()))
                report['paused_token'] = token
                # include a job hint if available
                try:
                    hj = paused_sessions.get(token, {}).get('job_hint')
                    report['paused_job_hint'] = hj
                except Exception:
                    report['paused_job_hint'] = None
                base = os.getenv('BASE_URL') or (request.url_root.rstrip('/') if request else None)
                if base:
                    report['paused_submit_url'] = f"{base}/submit-otp?token={token}"
                else:
                    report['paused_submit_url'] = f"/submit-otp?token={token}"
            except Exception:
                pass
    except Exception:
        pass
    return jsonify(report)


@app.route('/screenshot/latest')
def latest_screenshot():
    """Return the latest live screenshot (data/live.png) if available."""
    p = os.path.join('data', 'live.png')
    if not os.path.exists(p):
        return jsonify({'ok': False, 'error': 'no-screenshot'}), 404
    try:
        return send_file(p, mimetype='image/png')
    except Exception:
        logger.exception('Failed to serve latest screenshot')
        return jsonify({'ok': False, 'error': 'failed'}), 500


@app.route('/screenshot/token/<token>')
def screenshot_for_token(token):
    """Serve a paused-session screenshot saved under data/screenshots/{token}.png

    This is useful when live view isn't updating; admins can open this URL to download the exact
    screenshot captured when the session paused.
    """
    try:
        # If the paused session has a path recorded, prefer that
        ss_path = None
        try:
            if token in paused_sessions:
                ss_path = paused_sessions.get(token, {}).get('screenshot')
        except Exception:
            ss_path = None
        if not ss_path:
            ss_path = os.path.join('data', 'screenshots', f'{token}.png')
        if not os.path.exists(ss_path):
            return jsonify({'ok': False, 'error': 'screenshot not found', 'path': ss_path}), 404
        return send_file(ss_path, mimetype='image/png')
    except Exception:
        logger.exception('Failed to serve token screenshot')
        return jsonify({'ok': False, 'error': 'failed'}), 500


@app.route('/stop', methods=['POST'])
def stop_scraper():
    """Allow the user to immediately stop the running scraper."""
    try:
        if scraper_status.get('is_running'):
            stop_event.set()
            scraper_status['progress'] = 'Stop requested — attempting to cancel...'
            return jsonify({'ok': True, 'message': 'Stopping...'}), 200
        else:
            return jsonify({'ok': False, 'message': 'No scraper running'}), 400
    except Exception:
        logger.exception('Failed to request stop')
        return jsonify({'ok': False, 'error': 'failed to request stop'}), 500


@app.route('/vnc/')
def vnc_page():
    """Simple page that hosts an embedded noVNC client or shows instructions when not configured."""
    # Prefer an explicit env variable passed in, else rely on Flask's environment
    ws = os.getenv('WEBSOCKIFY_URL') or ''
    # pass env mapping for template to access
    env = {'WEBSOCKIFY_URL': ws}
    return render_template('vnc.html', env=env)


@app.route('/test-smtp', methods=['GET'])
def test_smtp():
    """Test SMTP login using GMAIL_USER and GMAIL_PASS from environment. Returns JSON success/failure."""
    gmail_user = os.getenv('GMAIL_USER')
    gmail_pass = os.getenv('GMAIL_PASS')
    if not gmail_user or not gmail_pass:
        return jsonify({'ok': False, 'error': 'GMAIL_USER or GMAIL_PASS not set in environment'}), 400
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
        server.ehlo()
        server.login(gmail_user, gmail_pass)
        server.quit()
        return jsonify({'ok': True, 'message': 'SMTP login successful'})
    except smtplib.SMTPAuthenticationError as e:
        detail_raw = getattr(e, 'smtp_error', str(e))
        try:
            if isinstance(detail_raw, (bytes, bytearray)):
                detail = detail_raw.decode('utf-8', errors='ignore')
            else:
                detail = str(detail_raw)
        except Exception:
            detail = str(e)
        return jsonify({'ok': False, 'error': 'authentication failed', 'detail': detail}), 401
    except Exception as e:
        return jsonify({'ok': False, 'error': 'unexpected error', 'detail': str(e)}), 500


@app.route('/test-ai', methods=['GET'])
def test_ai():
    """Quick connectivity check to Gemini."""
    if not GEMINI_API_KEY or not GEMINI_API_URL:
        return jsonify({'ok': False, 'error': 'GEMINI_API_KEY or GEMINI_API_URL not set'}), 400
    keep, reason = ai_is_usa_hiring_post("We are hiring a Software Engineer in the United States. Remote in US only.")
    return jsonify({'ok': True, 'keep': keep, 'reason': reason})


@app.route('/admin/sent-jobs', methods=['GET'])
@admin_required
def admin_list_sent_jobs():
    """Return a JSON list of sent jobs (payloads)."""
    try:
        jobs = get_all_sent_jobs()
        return jsonify({'ok': True, 'count': len(jobs), 'jobs': jobs})
    except Exception:
        logger.exception('Admin: Failed to list sent jobs')
        return jsonify({'ok': False, 'error': 'failed to list sent jobs'}), 500


@app.route('/admin/sent-jobs/clear', methods=['POST'])
@admin_required
def admin_clear_sent_jobs():
    """Clear all sent_jobs from the DB. Use with caution."""
    try:
        # simple implementation: delete rows directly from DB
        import sqlite3
        from db import DEFAULT_DB_PATH
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        cur = conn.cursor()
        cur.execute('DELETE FROM sent_jobs')
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'cleared': True})
    except Exception:
        logger.exception('Admin: Failed to clear sent jobs')
        return jsonify({'ok': False, 'error': 'failed to clear sent jobs'}), 500


@app.route('/admin/backup', methods=['POST'])
@admin_required
def admin_backup_db():
    """Create a timestamped backup of the SQLite DB and return the backup path."""
    try:
        import shutil
        from datetime import datetime
        info = __import__('db').db_info()
        src = info['path']
        if not src or not os.path.exists(src):
            return jsonify({'ok': False, 'error': 'source DB not found', 'path': src}), 400
        dst_dir = os.path.dirname(src)
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        dst = os.path.join(dst_dir, f'app.db.backup.{ts}')
        shutil.copy2(src, dst)
        logger.info(f'Admin: Created DB backup at {dst}')
        return jsonify({'ok': True, 'backup': dst})
    except Exception:
        logger.exception('Admin: Failed to create DB backup')
        return jsonify({'ok': False, 'error': 'failed to create backup'}), 500


def _create_pre_delete_backup(prefix: str) -> str | None:
    """Create a timestamped backup with the given prefix in the DB directory.

    Returns the backup path or None on failure.
    """
    try:
        import shutil
        info = __import__('db').db_info()
        src = info.get('path')
        if not src or not os.path.exists(src):
            return None
        dst_dir = os.path.dirname(src)
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        dst = os.path.join(dst_dir, f'app.db.backup.{prefix}.{ts}')
        shutil.copy2(src, dst)
        logger.info(f'Admin: Created pre-delete backup at {dst}')
        return dst
    except Exception:
        logger.exception('Admin: Pre-delete backup failed')
        return None


@app.route('/admin/backups/latest', methods=['GET'])
@admin_required
def admin_latest_backup():
    """Return the latest backup file path and a download URL."""
    try:
        info = __import__('db').db_info()
        dbdir = os.path.dirname(info.get('path') or '')
        if not dbdir or not os.path.exists(dbdir):
            return jsonify({'ok': False, 'error': 'db dir not found'}), 400
        files = [f for f in os.listdir(dbdir) if f.startswith('app.db.backup')]
        if not files:
            return jsonify({'ok': False, 'error': 'no backups found'}), 404
        files = sorted(files, key=lambda fn: os.path.getmtime(os.path.join(dbdir, fn)), reverse=True)
        latest = files[0]
        return jsonify({'ok': True, 'latest': os.path.join(dbdir, latest), 'download': f"/admin/backups/download?file={latest}"})
    except Exception:
        logger.exception('Admin: Failed to get latest backup')
        return jsonify({'ok': False, 'error': 'failed to get latest backup'}), 500


@app.route('/admin/backups/download', methods=['GET'])
@admin_required
def admin_download_backup():
    """Serve a backup file from the DB directory. Query param: file=<basename>"""
    fname = request.args.get('file')
    if not fname:
        return jsonify({'ok': False, 'error': 'file param required'}), 400
    try:
        info = __import__('db').db_info()
        dbdir = os.path.dirname(info.get('path') or '')
        # sanitize: only allow files that start with expected prefix
        if not fname.startswith('app.db.backup'):
            return jsonify({'ok': False, 'error': 'invalid file'}), 400
        full = os.path.join(dbdir, fname)
        if not os.path.exists(full):
            return jsonify({'ok': False, 'error': 'file not found'}), 404
        return send_file(full, as_attachment=True)
    except Exception:
        logger.exception('Admin: Failed to serve backup file')
        return jsonify({'ok': False, 'error': 'failed to serve file'}), 500


@app.route('/admin/sent-jobs/delete-old', methods=['POST'])
@admin_required
def admin_delete_sent_jobs_older():
    """Delete sent_jobs older than given number of days. JSON body: {"days": 30}

    Returns JSON with deleted count.
    """
    try:
        req = request.get_json(force=True, silent=True) or {}
        days = int(req.get('days', 30))
        cutoff = datetime.utcnow() - timedelta(days=days)
        # fetch ids and created_at
        import sqlite3
        from db import DEFAULT_DB_PATH
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT id, created_at FROM sent_jobs')
        rows = cur.fetchall()
        to_delete = []
        for r in rows:
            jid, created = r[0], r[1]
            if not created:
                continue
            # strip trailing Z if present
            ts = created.rstrip('Z')
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                # skip unparsable
                continue
            if dt < cutoff:
                to_delete.append(jid)
        # create a backup before destructive operation
        backup_path = _create_pre_delete_backup('before_delete_old')

        deleted = 0
        if to_delete:
            placeholders = ','.join(['?'] * len(to_delete))
            cur.execute(f'DELETE FROM sent_jobs WHERE id IN ({placeholders})', to_delete)
            deleted = cur.rowcount
            conn.commit()
        conn.close()
        return jsonify({'ok': True, 'deleted': deleted, 'backup': backup_path})
    except Exception:
        logger.exception('Admin: Failed to delete sent jobs by age')
        return jsonify({'ok': False, 'error': 'failed to delete by age'}), 500


@app.route('/admin/sent-jobs/delete', methods=['POST'])
@admin_required
def admin_delete_sent_jobs_by_ids():
    """Delete specific sent_jobs by id. JSON body: {"ids": ["id1","id2"]}
    """
    try:
        req = request.get_json(force=True, silent=True) or {}
        ids = req.get('ids') or []
        if not ids:
            return jsonify({'ok': False, 'error': 'no ids provided'}), 400
        # create pre-delete backup
        backup_path = _create_pre_delete_backup('before_delete_ids')

        import sqlite3
        from db import DEFAULT_DB_PATH
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        cur = conn.cursor()
        placeholders = ','.join(['?'] * len(ids))
        cur.execute(f'DELETE FROM sent_jobs WHERE id IN ({placeholders})', ids)
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'deleted': deleted, 'backup': backup_path})
    except Exception:
        logger.exception('Admin: Failed to delete sent jobs by ids')
        return jsonify({'ok': False, 'error': 'failed to delete by ids'}), 500


@app.route('/admin/sent-jobs/delete-by-email', methods=['POST'])
@admin_required
def admin_delete_sent_jobs_by_email():
    """Delete sent_jobs whose payload 'emails' array contains the given email.
    JSON body: {"email": "user@example.com"}
    """
    try:
        req = request.get_json(force=True, silent=True) or {}
        target = (req.get('email') or '').strip().lower()
        if not target:
            return jsonify({'ok': False, 'error': 'no email provided'}), 400
        # create pre-delete backup
        backup_path = _create_pre_delete_backup('before_delete_email')

        # load all jobs and inspect payloads
        import sqlite3, json
        from db import DEFAULT_DB_PATH
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT id, payload FROM sent_jobs')
        rows = cur.fetchall()
        to_delete = []
        for r in rows:
            jid, payload = r[0], r[1]
            try:
                obj = json.loads(payload)
                emails = obj.get('emails') or []
                if any(target == (e or '').strip().lower() for e in emails):
                    to_delete.append(jid)
            except Exception:
                continue
        deleted = 0
        if to_delete:
            placeholders = ','.join(['?'] * len(to_delete))
            cur.execute(f'DELETE FROM sent_jobs WHERE id IN ({placeholders})', to_delete)
            deleted = cur.rowcount
            conn.commit()
        conn.close()
        return jsonify({'ok': True, 'deleted': deleted, 'backup': backup_path})
    except Exception:
        logger.exception('Admin: Failed to delete sent jobs by email')
        return jsonify({'ok': False, 'error': 'failed to delete by email'}), 500


@app.route('/admin/senders', methods=['GET'])
@admin_required
def admin_list_senders():
    """Return the configured senders from settings (without revealing passwords in full)."""
    try:
        settings_local = load_settings() or {}
        senders = settings_local.get('senders') or []
        # Return masked senders (mask passwords)
        masked = []
        for s in senders:
            masked.append({
                'user': s.get('user') or s.get('email'),
                'host': s.get('host'),
                'port': s.get('port'),
                'use_ssl': bool(s.get('use_ssl')),
                'masked_pass': '****' if (s.get('pass') or s.get('password')) else ''
            })
        return jsonify({'ok': True, 'senders': masked})
    except Exception:
        logger.exception('Admin: Failed to list senders')
        return jsonify({'ok': False, 'error': 'failed to list senders'}), 500


@app.route('/admin/senders', methods=['POST'])
@admin_required
def admin_add_sender():
    """Add a sender entry to settings. Expect JSON: {user, pass, host, port, use_ssl}"""
    try:
        req = request.get_json(force=True, silent=True) or {}
        user = req.get('user') or req.get('email')
        pwd = req.get('pass') or req.get('password')
        if not user or not pwd:
            return jsonify({'ok': False, 'error': 'user and pass required'}), 400
        host = req.get('host') or os.getenv('SMTP_HOST') or 'smtp.gmail.com'
        port = int(req.get('port') or os.getenv('SMTP_PORT') or 465)
        use_ssl = bool(req.get('use_ssl') if 'use_ssl' in req else True)
        settings_local = load_settings() or {}
        senders = settings_local.get('senders') or []
        senders.append({'user': user, 'pass': pwd, 'host': host, 'port': port, 'use_ssl': use_ssl})
        settings_local['senders'] = senders
        save_settings(settings_local)
        return jsonify({'ok': True, 'senders': [{'user': x.get('user') or x.get('email'), 'host': x.get('host')} for x in senders]})
    except Exception:
        logger.exception('Admin: Failed to add sender')
        return jsonify({'ok': False, 'error': 'failed to add sender'}), 500


@app.route('/admin/senders', methods=['DELETE'])
@admin_required
def admin_delete_sender():
    """Delete a sender by email/user or by index. JSON body: {user: 'a@b.com'} or {index: 0}"""
    try:
        req = request.get_json(force=True, silent=True) or {}
        settings_local = load_settings() or {}
        senders = settings_local.get('senders') or []
        if 'index' in req:
            idx = int(req.get('index'))
            if 0 <= idx < len(senders):
                senders.pop(idx)
            else:
                return jsonify({'ok': False, 'error': 'index out of range'}), 400
        elif 'user' in req or 'email' in req:
            target = (req.get('user') or req.get('email')).strip().lower()
            new = [s for s in senders if ((s.get('user') or s.get('email') or '').strip().lower() != target)]
            if len(new) == len(senders):
                return jsonify({'ok': False, 'error': 'user not found'}), 404
            senders = new
        else:
            return jsonify({'ok': False, 'error': 'index or user required'}), 400
        settings_local['senders'] = senders
        save_settings(settings_local)
        return jsonify({'ok': True, 'senders': [{'user': x.get('user') or x.get('email'), 'host': x.get('host')} for x in senders]})
    except Exception:
        logger.exception('Admin: Failed to delete sender')
        return jsonify({'ok': False, 'error': 'failed to delete sender'}), 500
@app.route('/admin/extracted-emails', methods=['GET'])
@admin_required
def admin_get_extracted_emails():
    """Return the extracted-emails.json contents (if present)."""
    try:
        if not os.path.exists(EXTRACTED_EMAILS_FILE):
            return jsonify({'ok': True, 'count': 0, 'emails': []})
        with open(EXTRACTED_EMAILS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f) or []
        return jsonify({'ok': True, 'count': len(data), 'emails': data})
    except Exception:
        logger.exception('Admin: Failed to read extracted emails')
        return jsonify({'ok': False, 'error': 'failed to read extracted emails'}), 500


@app.route('/admin/extracted-emails/clear', methods=['POST'])
@admin_required
def admin_clear_extracted_emails():
    """Clear the extracted emails file."""
    try:
        if os.path.exists(EXTRACTED_EMAILS_FILE):
            # backup first
            try:
                fd, tmp = tempfile.mkstemp(prefix='extracted-emails-', dir=os.path.dirname(os.path.abspath(EXTRACTED_EMAILS_FILE)) or '.', text=True)
                os.close(fd)
                os.replace(EXTRACTED_EMAILS_FILE, tmp)
                # remove backup; keep the tmp in case admin wants to recover (we leave it)
            except Exception:
                try:
                    os.remove(EXTRACTED_EMAILS_FILE)
                except Exception:
                    pass
        # reset status so UI shows zero
        try:
            scraper_status['extracted_emails_count'] = 0
            scraper_status['extracted_emails_file'] = ''
        except Exception:
            pass
        return jsonify({'ok': True})
    except Exception:
        logger.exception('Admin: Failed to clear extracted emails')
        return jsonify({'ok': False, 'error': 'failed to clear extracted emails'}), 500


@app.route('/admin/resume-scraper', methods=['POST'])
@admin_required
def admin_resume_scraper():
    """Resume a paused scraper that was waiting for human verification.

    Admins should open the app's browser window, complete any LinkedIn verification manually,
    then POST to this endpoint to allow the scraper to continue.
    """
    try:
        # If the scraper is currently paused, clear the paused flag so it resumes.
        if scraper_status.get('paused_for_human_verification'):
            scraper_status['paused_for_human_verification'] = False
            scraper_status['progress'] = 'Resumed by admin after manual human verification.'
        else:
            # Scraper hasn't set paused flag yet (race). Record that resume was requested so the scraper will honor it when it reaches the pause.
            scraper_status['resume_requested'] = True
            scraper_status['progress'] = 'Resume requested by admin; will continue if/when verification completes.'
        return jsonify({'ok': True, 'message': 'Scraper resume signal sent.'})
    except Exception:
        logger.exception('Admin: Failed to resume scraper')
        return jsonify({'ok': False, 'error': 'failed to resume'}), 500


@app.route('/submit-otp', methods=['GET'])
def submit_otp_form():
    token = request.args.get('token')
    if not token or token not in paused_sessions:
        return "Invalid or expired token.", 400
    # Simple HTML form for OTP entry
    return f"""
    <html><body>
    <h3>Enter the OTP from LinkedIn</h3>
    <p>This will submit the OTP to the paused scraping session so the job can resume.</p>
    <form action="/api/submit-otp" method="post">
      <input type="hidden" name="token" value="{token}" />
      OTP: <input name="otp" />
      <button type="submit">Submit OTP</button>
    </form>
    </body></html>
    """


@app.route('/api/submit-otp', methods=['POST'])
def api_submit_otp():
    data = request.get_json(silent=True) or request.form or {}
    token = data.get('token')
    otp = data.get('otp')
    if not token or token not in paused_sessions:
        return jsonify({'ok': False, 'error': 'invalid or expired token'}), 400
    if not otp:
        return jsonify({'ok': False, 'error': 'otp required'}), 400
    sess = paused_sessions.get(token)
    driver = sess.get('driver_ref') if sess else None
    if not driver:
        return jsonify({'ok': False, 'error': 'server-side browser session not available'}), 500
    try:
        injected = False
        # Attempt common OTP input selectors
        try:
            candidates = driver.find_elements(By.XPATH, "//input[@name='pin' or @name='otp' or @id='otp' or @type='tel' or @type='text']")
        except Exception:
            candidates = []
        for c in candidates:
            try:
                c.clear()
                c.send_keys(otp)
                injected = True
            except Exception:
                continue
        if not injected:
            try:
                active = driver.switch_to.active_element
                active.clear()
                active.send_keys(otp)
                injected = True
            except Exception:
                injected = False

        if not injected:
            return jsonify({'ok': False, 'error': 'failed to inject otp: no suitable input found'}), 500

        # Try to click verify/submit buttons
        try:
            buttons = driver.find_elements(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'verify') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue')]")
            for b in buttons[:3]:
                try:
                    b.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", b)
                    except Exception:
                        pass
        except Exception:
            pass

        # Signal scraper to resume; scraper loop checks page state and will continue
        scraper_status['resume_requested'] = True
        try:
            del paused_sessions[token]
        except Exception:
            pass
        return jsonify({'ok': True, 'message': 'OTP submitted; scraper will attempt to resume.'})
    except Exception as e:
        logger.exception('Error injecting OTP into paused session')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/remote/click', methods=['POST'])
def api_remote_click():
    """Remote click into paused Selenium session.

    Expected JSON: {token: str, x: float, y: float}
    x,y are normalized coords (0..1) relative to the served live screenshot image.
    The server maps them into the page viewport using JS-exposed window.innerWidth/innerHeight
    and attempts a native click via ActionChains. Falls back to elementFromPoint+dispatchEvent.
    """
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    if not token or token not in paused_sessions:
        return jsonify({'ok': False, 'error': 'invalid or expired token'}), 400
    try:
        x = float(data.get('x'))
        y = float(data.get('y'))
    except Exception:
        return jsonify({'ok': False, 'error': 'x and y (normalized floats) required'}), 400
    sess = paused_sessions.get(token)
    driver = sess.get('driver_ref') if sess else None
    if not driver:
        return jsonify({'ok': False, 'error': 'server-side browser session not available'}), 500
    try:
        # Ask the page for its viewport size via JS so we can convert normalized coords
        try:
            vw = driver.execute_script('return window.innerWidth||document.documentElement.clientWidth')
            vh = driver.execute_script('return window.innerHeight||document.documentElement.clientHeight')
        except Exception:
            vw = None
            vh = None
        if vw and vh:
            px = int(max(0, min(1, x)) * int(vw))
            py = int(max(0, min(1, y)) * int(vh))
        else:
            # Fallback: assume 1920x1080
            px = int(max(0, min(1, x)) * 1920)
            py = int(max(0, min(1, y)) * 1080)

        # Try a native Selenium click by computing element at point using JS
        performed = False
        try:
            # Try elementFromPoint to get the element and scroll into view then click via ActionChains
            el = driver.execute_script('return document.elementFromPoint(arguments[0], arguments[1]);', px, py)
            if el:
                # We have an element reference; try to click it natively
                try:
                    # Move to location and click
                    ActionChains(driver).move_by_offset(0, 0).perform()
                except Exception:
                    pass
                try:
                    # Use JS to click the element directly if possible
                    driver.execute_script('arguments[0].scrollIntoView({block:"center"});', el)
                except Exception:
                    pass
                try:
                    ActionChains(driver).move_to_element_with_offset(el, 1, 1).click().perform()
                    performed = True
                except Exception:
                    try:
                        driver.execute_script('arguments[0].click();', el)
                        performed = True
                    except Exception:
                        performed = False
        except Exception:
            performed = False

        # If the above didn't work, try dispatching a pointer event at page coordinates
        if not performed:
            try:
                js = (
                    'var ev = new MouseEvent("click", {bubbles:true, cancelable:true, clientX:arguments[0], clientY:arguments[1]});'
                    'var el = document.elementFromPoint(arguments[0], arguments[1]); if(el) el.dispatchEvent(ev); return !!el;'
                )
                ok = driver.execute_script(js, px, py)
                performed = bool(ok)
            except Exception:
                performed = False

        # Save a live screenshot so UI can refresh immediately
        try:
            _save_live_screenshot(driver)
        except Exception:
            pass

        if performed:
            return jsonify({'ok': True, 'message': 'clicked', 'px': px, 'py': py})
        else:
            return jsonify({'ok': False, 'error': 'click failed'}), 500
    except Exception as e:
        logger.exception('api_remote_click error')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/remote/type', methods=['POST'])
def api_remote_type():
    """Send text input to the active/focused element in the paused session.

    Expected JSON: {token: str, text: str}
    """
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    text = data.get('text')
    if not token or token not in paused_sessions:
        return jsonify({'ok': False, 'error': 'invalid or expired token'}), 400
    if text is None:
        return jsonify({'ok': False, 'error': 'text required'}), 400
    sess = paused_sessions.get(token)
    driver = sess.get('driver_ref') if sess else None
    if not driver:
        return jsonify({'ok': False, 'error': 'server-side browser session not available'}), 500
    try:
        try:
            active = driver.switch_to.active_element
            active.click()
            active.clear()
            active.send_keys(text)
        except Exception:
            # Fallback: focus body and send keys
            try:
                driver.execute_script('document.activeElement && document.activeElement.blur && document.activeElement.blur();')
            except Exception:
                pass
            try:
                driver.execute_script('var i = document.querySelector("input[type=text], input[type=tel], input[name*=otp], textarea"); if(i){ i.focus(); }')
                el = driver.switch_to.active_element
                el.send_keys(text)
            except Exception:
                driver.execute_script('console.warn("remote type fallback failed")')
                return jsonify({'ok': False, 'error': 'failed to type into element'}), 500
        try:
            _save_live_screenshot(driver)
        except Exception:
            pass
        return jsonify({'ok': True, 'message': 'typed'})
    except Exception as e:
        logger.exception('api_remote_type error')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/remote/key', methods=['POST'])
def api_remote_key():
    """Send a special key (Enter, Tab, Escape) to the active element.

    Expected JSON: {token: str, key: 'Enter'|'Tab'|'Escape'}
    """
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    key = data.get('key')
    if not token or token not in paused_sessions:
        return jsonify({'ok': False, 'error': 'invalid or expired token'}), 400
    if not key:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    sess = paused_sessions.get(token)
    driver = sess.get('driver_ref') if sess else None
    if not driver:
        return jsonify({'ok': False, 'error': 'server-side browser session not available'}), 500
    try:
        k = key.lower()
        from selenium.webdriver.common.keys import Keys as SKeys
        mapping = {
            'enter': SKeys.ENTER,
            'tab': SKeys.TAB,
            'escape': SKeys.ESCAPE,
            'esc': SKeys.ESCAPE,
        }
        send = mapping.get(k)
        if not send:
            return jsonify({'ok': False, 'error': 'unsupported key'}), 400
        try:
            active = driver.switch_to.active_element
            active.send_keys(send)
        except Exception:
            try:
                driver.execute_script('document.activeElement && document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: arguments[0]}));', key)
            except Exception:
                return jsonify({'ok': False, 'error': 'failed to send key'}), 500
        try:
            _save_live_screenshot(driver)
        except Exception:
            pass
        return jsonify({'ok': True, 'message': 'key sent'})
    except Exception as e:
        logger.exception('api_remote_key error')
        return jsonify({'ok': False, 'error': str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))