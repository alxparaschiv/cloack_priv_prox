FROM python:3.11-slim

# System libs for Camoufox (Firefox) + Playwright (Chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
    xvfb \
    libgtk-3-0 \
    libx11-xcb1 \
    libasound2 \
    libdbus-glib-1-2 \
    libxt6 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Camoufox browser binary (used by proxy.py's check_recaptcha_score /
# check_google_hello / test_proxy_on_facebook gates)
RUN python -m camoufox fetch

# Playwright Chromium (fallback used by some gates)
RUN playwright install chromium --with-deps

# Copy application
COPY . .

CMD ["python", "bot.py"]
