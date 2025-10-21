# Dockerfile.render - single-container for Flask + Chrome (Xvfb) + VNC + noVNC + nginx
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# Set default ports used internally
ENV FLASK_PORT=8080
ENV NOVNC_PORT=6081
ENV VNC_PORT=5900

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg2 apt-transport-https fonts-liberation \
    wget unzip xvfb x11vnc net-tools supervisor nginx python3 python3-venv python3-pip \
    git build-essential libnss3 libatk-bridge2.0-0 libgtk-3-0 libxss1 libasound2 libx11-xcb1 \
 && rm -rf /var/lib/apt/lists/*

# Install Google Chrome stable
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable
ENV CHROME_BIN=/usr/bin/google-chrome

# Install chromedriver from Ubuntu repo (matches Google Chrome stable closely)
RUN apt-get update && apt-get install -y chromium-chromedriver
ENV CHROMEDRIVER_PATH=/usr/lib/chromium-browser/chromedriver
ENV PATH="/opt/chromedriver:${PATH}"

# Create app user
RUN useradd -m -s /bin/bash appuser
WORKDIR /home/appuser/app
COPY . /home/appuser/app
RUN chown -R appuser:appuser /home/appuser/app

# python deps
RUN python3 -m pip install --upgrade pip
RUN python3 -m pip install -r requirements.txt

# Install websockify and noVNC client (we'll use upstream noVNC static files)
RUN python3 -m pip install websockify==0.10.0
RUN mkdir -p /opt/novnc && \
    curl -fsSL https://github.com/novnc/noVNC/archive/refs/heads/master.zip -o /tmp/novnc.zip \
    && apt-get update && apt-get install -y unzip && unzip /tmp/novnc.zip -d /opt && mv /opt/noVNC-master /opt/novnc && rm /tmp/novnc.zip


# Copy nginx and supervisord confs
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose nothing explicitly (Render provides $PORT). For local testing we expose ports too:
EXPOSE 8080 6081 6901 5900

USER appuser

# Use supervisord to run processes (nginx forwarded by root by supervisord entrypoint)
ENTRYPOINT ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

WORKDIR /home/appuser/app
RUN mkdir -p /home/appuser/logs && chown -R appuser:appuser /home/appuser/logs
