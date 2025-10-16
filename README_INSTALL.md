LinkedIn Scraper - Packaging & Install Guide

This document describes how to create a client-ready package, install prerequisites, configure the app for a client machine, and run it as a background service on Windows.

Quick summary (one-liner):
- Create release ZIP (packaging script included).
- Install Python & dependencies, set `ADMIN_TOKEN` and `DB_PATH` in `.env`.
- Use Waitress to run the app in production or register the included service wrapper (NSSM) to auto-start.

Files included for packaging
- `package_release.ps1` — creates `LinkedInScraper-release.zip` in the parent folder (excludes common junk folders).
- `install_service.ps1` — helper script to register the app as a Windows service using NSSM; when NSSM is not present it prints a Scheduled Task fallback command.
- `backup_db.py` — simple CLI to create a timestamped DB backup.
- `wsgi.py` — WSGI entrypoint for production (used by Waitress).

Step-by-step instructions (client machine)

1) Copy the release ZIP to the client machine and extract it to a folder, e.g. `C:\LinkedInScraper`.

2) Install Python 3.11+ if not installed. Ensure `python` and `pip` are on PATH.

3) From the project folder install Python dependencies once (use an elevated prompt if needed):

```powershell
pip install -r .\requirements.txt
```

4) Configure environment and .env
- Create a `.env` file in the project folder or set environment variables system-wide.
- Required variables:
  - `ADMIN_TOKEN` — a secret token used to protect admin endpoints.
- Optional variables:
  - `DB_PATH` — set where to store the SQLite DB. Recommended: `%LOCALAPPDATA%\linkedin-scraper\app.db`.

Example `.env`:
```
ADMIN_TOKEN=replace-with-secret-token
DB_PATH=C:\Users\<user>\AppData\Local\linkedin-scraper\app.db
GMAIL_USER=youremail@example.com
GMAIL_PASS=your-google-app-password
```

5) Create the DB folder and initialize DB (one-time):
```powershell
New-Item -ItemType Directory -Path "$env:LOCALAPPDATA\linkedin-scraper" -Force
# optional: move an existing app.db if upgrading
# If you want to initialize, run:
python -c "import db; db.init_db(); print('DB initialized at', db.db_info())"
```

6) Run the app with a production WSGI server (Waitress recommended on Windows):

```powershell
# run in the session where .env has been loaded (or set the env vars first)
python -c "from waitress import serve; import wsgi; serve(wsgi.app, listen='127.0.0.1:5001')"
```

7) Optional: install as a Windows service (auto-start) using `install_service.ps1`.

Service options
- Recommended approach: install NSSM (Non-Sucking Service Manager) and use `install_service.ps1 -Action install -ServiceName LinkedInScraper`.
- If NSSM is not available, the script prints a `schtasks` command you can use as a fallback.

Security & operations notes
- Keep `ADMIN_TOKEN` secret; share it with admins only.
- Use a per-machine `DB_PATH` to ensure client data stays on each employee device.
- Do not store secrets in OneDrive or a shared folder.
- Add backup automation if you need scheduled DB backups (use `backup_db.py`).

Support
- If you want, I can produce a zipped release and a short install video walkthrough. Ask for "create zip" and I will generate the package script output steps for you to run.
