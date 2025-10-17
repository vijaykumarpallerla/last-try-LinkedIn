FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Ensure the start script is executable and use it so we can bind to $PORT provided by Render
RUN chmod +x ./start.sh || true
CMD ["./start.sh"]
