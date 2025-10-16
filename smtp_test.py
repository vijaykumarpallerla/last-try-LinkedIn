<<<<<< HEAD
import os
import smtplib
import sys
@@ -65,3 +66,72 @@

print('\nAll attempts failed. If you used an App Password, double-check you copied the exact 16-character code (no extra spaces/newlines).')
sys.exit(1)
=======
import os
import smtplib
import sys
from pathlib import Path

# Try to load .env if python-dotenv installed, else parse manually
env_path = Path(_file_).parent / '.env'
print('DEBUG: .env path ->', env_path, 'exists:', env_path.exists())
if env_path.exists():
    try:
        from dotenv import load_dotenv
        print('DEBUG: python-dotenv present, calling load_dotenv')
        res = load_dotenv(env_path)
        print('DEBUG: load_dotenv returned ->', res)
    except Exception:
        # manual parse
        print('DEBUG: .env exists, manual parse engaged')
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        print('DEBUG: raw .env lines:')
        for i, raw in enumerate(lines, start=1):
            print(i, repr(raw))
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"\'')
                os.environ[k] = v
                print(f"DEBUG: set env {k} -> {repr(v)}")

print('DEBUG: env values after load/manual-parse:')
for key in ('GMAIL_USER', 'GMAIL_PASS'):
    print(key, '->', repr(os.getenv(key)))

GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASS = os.getenv('GMAIL_PASS')

if not GMAIL_USER or not GMAIL_PASS:
    print('ERROR: GMAIL_USER or GMAIL_PASS not found in environment/.env')
    sys.exit(2)

candidates = [GMAIL_PASS]
# Also try removing spaces (common when copying app passwords grouped in 4)
collapsed = GMAIL_PASS.replace(' ', '')
if collapsed != GMAIL_PASS:
    candidates.append(collapsed)

print('Testing SMTP login for:', GMAIL_USER)
for idx, pwd in enumerate(candidates, start=1):
    label = 'original' if idx == 1 else 'spaces-removed'
    print(f"Trying password variant: {label}")
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
            smtp.ehlo()
            smtp.login(GMAIL_USER, pwd)
        print('SUCCESS: Logged in to smtp.gmail.com with variant:', label)
        sys.exit(0)
    except smtplib.SMTPAuthenticationError as e:
        # e.smtp_code, e.smtp_error
        print('AUTH ERROR for variant', label, '->', getattr(e, 'smtp_error', str(e)))
    except Exception as e:
        print('ERROR for variant', label, '->', repr(e))

print('\nAll attempts failed. If you used an App Password, double-check you copied the exact 16-character code (no extra spaces/newlines).')
sys.exit(1)
>>>>>>> e65c6e7abcd3efc862f928cf2862db1ff5080d0a
