#!/usr/bin/env bash
set -e

# Default port Render provides via $PORT, fallback to 8000
PORT=${PORT:-8000}

# Ensure database and data directories exist
mkdir -p /app/data

# Run migrations or setup if you have any (no-op placeholder)
echo "Starting app on port $PORT"

# Diagnostic: print chrome/chromium binary info to logs for debugging on Render
echo "CHROME_BIN=${CHROME_BIN:-undefined}"
echo "which chromium:"; which chromium || true
echo "which chromium-browser:"; which chromium-browser || true
echo "ls /usr/bin/chromium*:"; ls -l /usr/bin/chromium* 2>/dev/null || true
echo "$CHROME_BIN --version:"; if [ -n "${CHROME_BIN}" ] && [ -x "${CHROME_BIN}" ]; then "${CHROME_BIN}" --version || true; fi


# Start gunicorn serving app:app (adjust if your app object is named differently)
exec gunicorn --bind 0.0.0.0:${PORT} wsgi:app --workers 2 --threads 4 --timeout 120
