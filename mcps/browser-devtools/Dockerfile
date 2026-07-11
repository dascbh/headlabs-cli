# Base on the official Playwright Python image (Chromium + all system libs
# preinstalled, multi-arch incl. arm64) — same recipe the platform render-service
# uses to run Chromium in a container. Pin matches requirements.txt (1.48.0).
FROM --platform=linux/arm64 mcr.microsoft.com/playwright/python:v1.48.0-jammy
ENV MCP_ID=browser-devtools PORT=8000 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Ensure the Chromium build matching the pinned Playwright is present.
RUN python -m playwright install chromium
COPY . /app
EXPOSE 8000
CMD ["python", "server.py"]
