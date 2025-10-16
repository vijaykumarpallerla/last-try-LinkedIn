=====
LinkedIn group scraper — sends LinkedIn group posts to email

Quick start

1. Create and activate a virtualenv (Windows PowerShell):

powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt


2. Create a .env file with your Gmail credentials (use an App Password):


GMAIL_USER=your@gmail.com
GMAIL_PASS=your_app_password_here


3. Start the app:

powershell
& '.\.venv\Scripts\python.exe' .\app.py


4. Open http://127.0.0.1:5001 in your browser and configure groups/recipients.

Notes

- Use a Google App Password (not your normal Gmail password) for GMAIL_PASS.
- sent-jobs.json stores which posts were sent. Do not commit that file.
- If you'd like me to create a GitHub repo for you and push, provide the remote URL or give permission and I will add and push the repo.
>>>>>>> e65c6e7abcd3efc862f928cf2862db1ff5080d0a
