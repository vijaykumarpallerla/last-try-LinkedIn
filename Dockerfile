FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Use startup script so we can bind to $PORT provided by Render
CMD ["./start.sh"]
