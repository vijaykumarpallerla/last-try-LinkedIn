Database setup and local storage

This project uses a local SQLite database (`app.db` by default) to store application settings and records of sent jobs.

Where the DB is stored

- Default: `app.db` in the project's working directory.
- Override: set `DB_PATH` environment variable or add it to `.env` in the project folder:

  DB_PATH=C:\Users\<user>\AppData\Local\linkedin-scraper\app.db

Make the DB local (recommended for each employee)

1. Create a local folder (one per machine):

   New-Item -ItemType Directory -Path "$env:LOCALAPPDATA\linkedin-scraper" -Force

2. Move the DB to that folder (if it exists in the project folder):

   Move-Item -Path ".\app.db" -Destination "$env:LOCALAPPDATA\linkedin-scraper\app.db" -Force

3. Set `DB_PATH` for the session and start the app (PowerShell session):

   $env:DB_PATH = "$env:LOCALAPPDATA\linkedin-scraper\app.db"
   python .\app.py

Or persist `DB_PATH` in the `.env` file on each machine.

Security notes

- If the project directory is in OneDrive, `app.db` will be synced. This may expose data to the cloud and other devices. Move the DB to a non-synced local folder for privacy.
- Keep secrets (GMAIL credentials) out of committed files. Use `.env` and don't commit it.

If you want, I can add an admin endpoint to view and clear `sent_jobs`, or add a small CLI script to backup/restore the DB.
