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

# Ensure common chromium binary names exist and set CHROME_BIN so the app can find Chromium
RUN if [ -x /usr/bin/chromium-browser ]; then ln -sf /usr/bin/chromium-browser /usr/bin/chromium; \
	elif [ -x /usr/bin/chromium ]; then ln -sf /usr/bin/chromium /usr/bin/chromium-browser; fi
ENV CHROME_BIN=/usr/bin/chromium-browser
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
