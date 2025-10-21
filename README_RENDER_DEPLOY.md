# Deploying to Render (guide)

1. Create a new Web Service on Render and connect your GitHub repo.
2. Set the service to use Docker, point Dockerfile path to `Dockerfile` and branch `main`.
3. Ensure the container listens on port 8080 (Render sets $PORT; we use nginx listening on 8080).
4. Add environment variables in the Render dashboard (secrets):
   - GMAIL_USER, GMAIL_PASS, RECIPIENTS, LINKEDIN_USER, LINKEDIN_PASS, ADMIN_TOKEN
   - WEBSOCKIFY_URL = /vnc/vnc.html?host=127.0.0.1&port=5900
5. Choose a machine with at least 2 CPU / 4GB RAM (Standard-2x recommended).
6. Deploy and monitor logs. Once healthy you can open the HTTPS service URL and press Start Scraping.

Security notes: use Render's environment variables for secrets; protect /vnc/ with basic auth or ADMIN_TOKEN.
