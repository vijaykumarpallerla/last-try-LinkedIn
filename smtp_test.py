#!/usr/bin/env python3
"""Simple SMTP login tester.

Loads environment from a local .env (if present) then attempts to authenticate
to smtp.gmail.com using GMAIL_USER and GMAIL_PASS. Exits 0 on success, non-zero
on failure.
"""
import os
import smtplib
import sys
from pathlib import Path


def load_dotenv_if_present():
    env_path = Path(__file__).parent / '.env'
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
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
                        os.environ.setdefault(k.strip(), v.strip().strip('\"\''))
        except Exception:
            pass


def main():
    load_dotenv_if_present()

    GMAIL_USER = os.getenv('GMAIL_USER')
    GMAIL_PASS = os.getenv('GMAIL_PASS')

    if not GMAIL_USER or not GMAIL_PASS:
        print('ERROR: GMAIL_USER or GMAIL_PASS not found in environment/.env')
        return 2

    candidates = [GMAIL_PASS]
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
            return 0
        except smtplib.SMTPAuthenticationError as e:
            print('AUTH ERROR for variant', label, '->', getattr(e, 'smtp_error', str(e)))
        except Exception as e:
            print('ERROR for variant', label, '->', repr(e))

    print('\nAll attempts failed. If you used an App Password, double-check you copied the exact 16-character code (no extra spaces/newlines).')
    return 1


if __name__ == '__main__':
    sys.exit(main())
