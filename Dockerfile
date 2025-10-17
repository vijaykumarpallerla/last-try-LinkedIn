FROM python:3.11-slim

WORKDIR /app

# Install system dependencies and Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
	ca-certificates \
	wget \
	gnupg2 \
	fonts-liberation \
	libnss3 \
	libatk-bridge2.0-0 \
	libgtk-3-0 \
	libx11-6 \
	libxss1 \
	libasound2 \
	libxrandr2 \
	libxdamage1 \
	libgbm1 \
	xvfb \
	chromium-driver \
	chromium \
 && rm -rf /var/lib/apt/lists/*

# Set CHROME_BIN so the app can find Chromium in container
ENV CHROME_BIN=/usr/bin/chromium
ENV PYTHONUNBUFFERED=1

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

EXPOSE 8000

# Ensure the start script is executable and use it so we can bind to $PORT provided by Render
RUN chmod +x ./start.sh || true
CMD ["./start.sh"]
