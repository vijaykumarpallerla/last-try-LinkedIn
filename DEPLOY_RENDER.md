Render deployment checklist (minimal)

1. Ensure repository has the latest code on `main` branch.

2. Set up Render service (Docker) using `render.yaml` or via Render UI:
   - Service type: Web Service
   - Environment: Docker
   - Branch: main
   - Dockerfile path: Dockerfile

3. In Render dashboard, set the following environment variables (do NOT commit these to git):
   - GMAIL_USER
   - GMAIL_PASS
   - LINKEDIN_USER (optional)
   - LINKEDIN_PASS (optional)
   - ADMIN_TOKEN
   - RECIPIENTS (optional)
   - PORT (Render provides this automatically)
   - If you want noVNC embedded from an external websockify, set WEBSOCKIFY_URL to its public URL.

4. Choose a sufficiently large instance (the Dockerfile comments recommend `standard-2x` or larger due to Chrome memory usage).

5. After deploy, check service logs for supervisord, gunicorn, and websockify logs.

6. Smoke test:
   - Open the service URL, configure settings if necessary, open `/vnc/` to view noVNC, and click Start Scraping.
   - Watch logs for `[SCRAPER-DEBUG]` messages.

Notes:
- Do not commit `.env` or any secrets. The included `.gitignore` already contains `.env`, `sent-jobs.json`, and other local artifacts.
- If you encounter Chromedriver/Chrome mismatches, we'll adjust the Dockerfile or use webdriver-manager in code.
