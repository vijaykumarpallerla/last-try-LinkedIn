Quick local noVNC test (Docker)

This uses dorowu/ubuntu-desktop-lxde-vnc which bundles a lightweight desktop, TigerVNC, and websockify/noVNC.

1) Prerequisites
- Docker installed.
- On Windows use Docker Desktop with WSL2.

2) Start the container

PowerShell / WSL / Linux:

```powershell
# from project root where docker-compose.novnc.yml lives
docker compose -f docker-compose.novnc.yml up -d
```

3) Open the noVNC client in your browser
- Default address: http://localhost:6901/
- VNC password: changeme (change in docker-compose for security)

4) Example WEBSOCKIFY_URL for this local test
- http://localhost:6901/vnc.html?host=127.0.0.1&port=5900

5) Use it with the app
- Set the environment variable then start your Flask app:

PowerShell:

```powershell
$env:WEBSOCKIFY_URL = 'http://localhost:6901/vnc.html?host=127.0.0.1&port=5900'
python .\app.py
```

6) Notes
- This is a minimal local test only. For sharing outside your machine you need TLS and authentication.
- Change VNC password by setting VNC_PASSWORD env in docker-compose.
- The dorowu image is convenient for testing; see image docs for configuration options.
