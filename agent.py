# agent.py (prototype)
import os, time, uuid, requests, sys, json
from pathlib import Path

SERVER_URL = os.environ.get("SERVER_URL", "https://your-server.example.com")
AGENT_ID = os.environ.get("AGENT_ID", f"agent-{uuid.uuid4().hex[:8]}")
AGENT_SECRET = os.environ.get("AGENT_SECRET", "change-me")  # pre-share or get from server

def register():
    r = requests.post(f"{SERVER_URL}/api/agents/register", json={
        "agent_id": AGENT_ID, "hostname": os.getenv("COMPUTERNAME","unknown")
    }, headers={"X-Agent-Secret": AGENT_SECRET}, timeout=10)
    r.raise_for_status()
    return r.json()

def poll_job(token):
    # long-poll endpoint for simplicity (server holds until job arrives or timeout)
    r = requests.get(f"{SERVER_URL}/api/agents/{AGENT_ID}/next-job", headers={
        "Authorization": f"Bearer {token}"
    }, timeout=70)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()

def post_result(token, job_id, result):
    r = requests.post(f"{SERVER_URL}/api/agents/{AGENT_ID}/results", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }, json={"job_id": job_id, "result": result}, timeout=10)
    r.raise_for_status()
    return r.json()

def run_playwright_scrape(job):
    # Minimal placeholder: in real agent, import playwright sync API and automate.
    # For now, just simulate work and return fake data.
    time.sleep(2)
    return {"status":"ok","found":[{"email":"alice@example.com","snippet":"hiring"}]}

def main():
    print("Agent starting, registering...")
    info = register()
    token = info["agent_token"]
    print("Registered. Agent token received. Entering poll loop.")
    while True:
        job = poll_job(token)
        if not job:
            continue
        print("Got job:", job)
        if job.get("command") == "scrape":
            try:
                result = run_playwright_scrape(job)
                post_result(token, job["job_id"], {"status":"success","data":result})
                print("Posted result")
            except Exception as e:
                post_result(token, job["job_id"], {"status":"failed","error":str(e)})
        elif job.get("command") == "stop":
            print("Received stop command.")
            break

if __name__ == "__main__":
    main()