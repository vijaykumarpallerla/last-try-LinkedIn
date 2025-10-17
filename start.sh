#!/usr/bin/env bash
set -e

# Default port Render provides via $PORT, fallback to 8000
PORT=${PORT:-8000}

# Ensure database and data directories exist
mkdir -p /app/data

# Run migrations or setup if you have any (no-op placeholder)
echo "Starting app on port $PORT"

# Start gunicorn serving app:app (adjust if your app object is named differently)
exec gunicorn --bind 0.0.0.0:${PORT} wsgi:app --workers 2 --threads 4 --timeout 120
