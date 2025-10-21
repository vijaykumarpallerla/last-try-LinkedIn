# Docker Compose PoC — interactive scraper with noVNC

This small PoC runs your Flask app alongside a Selenium "chrome-debug" container (which provides Chrome + noVNC) and an Nginx reverse proxy that exposes both the web UI and the noVNC client.

Files added:
- `docker-compose.yml` — runs `app`, `chrome-debug` and `nginx` services.
- `nginx.conf` — routes `/vnc/` to the chrome debug noVNC UI and `/` to the Flask app.

How it works
- `app` builds from the current repository Dockerfile and listens on port 8080.
- `chrome-debug` runs the `selenium/standalone-chrome-debug` image; noVNC is exposed on port 6901.
- `nginx` proxies `/vnc/` to `chrome-debug:6901` and `/` to `app:8080`.

Run locally (requires Docker & docker-compose):
1. Build and start containers:
```bash
docker compose up --build
```
2. Open the control panel in your browser:
- http://localhost/  (proxied to the Flask UI)

3. When your scraper pauses for human verification, open the VNC UI:
- http://localhost/vnc/  (noVNC web client for the chrome-debug container)

Expose to the public internet (optional)
--------------------------------------
If you want colleagues to open the live browser from outside your LAN, create a short-lived public tunnel. Two common tools are `ngrok` and `cloudflared` (Cloudflare Tunnel). This repo includes a small PowerShell helper `start_tunnel.ps1` that will try to start a tunnel and print the public URL.

Example (Windows PowerShell):
1. Start the Docker Compose stack (in one terminal):
```powershell
docker compose up --build
```
2. In another PowerShell window, run the helper (from the repo root):
```powershell
.\start_tunnel.ps1
```
3. If `ngrok` or `cloudflared` is available on your PATH, the script will print a public URL like `https://abcd-1234.ngrok.io` or `https://your-tunnel.example.com` — open that URL in a browser and append `/vnc/` to access the live Chrome noVNC client (for example `https://abcd-1234.ngrok.io/vnc/`).

Security notes:
- Tunnels expose your local services to the public internet. Do not expose this on an unsecured machine or network.
- Before making `/vnc/` public, add authentication (Nginx basic auth or OAuth) and enable TLS (ngrok and cloudflared provide TLS by default).
- Revoke or stop the tunnel when you are done.

Notes and security
- This PoC intentionally exposes noVNC on `/vnc/` without auth. Do NOT run this configuration in production without adding TLS and authentication (e.g., Nginx basic auth or OAuth2 Proxy in front of nginx).
- For production, prefer running on Kubernetes with proper Ingress + TLS + OAuth, or use a managed browser service.

Next steps
- If you want, I can:
  - Add a small Flask proxy to map generated `paused_token` values to a specific `/vnc/<token>` path instead of always exposing the single chrome-debug instance.
  - Add basic auth to nginx and an example `.htpasswd`.
  - Generate k8s manifests for a single interactive pod + ingress.
