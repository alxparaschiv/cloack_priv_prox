FROM python:3.11-slim

# System libs for Camoufox (Firefox) + Playwright (Chromium) + adb (Appium
# for GeeLark cloud-phone control plane). gnupg + openjdk-21 + android-tools-adb
# are NEW additions for /ig_setup_private (Shard A, 2026-06-04) — they don't
# affect any pre-existing functionality. nodejs comes in a separate RUN below
# (NodeSource APT setup).
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
    gnupg \
    openjdk-21-jre-headless \
    android-tools-adb \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 — required for Appium server. Separate RUN to keep the prior
# apt layer cacheable on rebuilds that don't touch Appium.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Appium server + UiAutomator2 driver (drives Instagram on the GeeLark Android
# cloud phone via the phone's exposed ADB endpoint). Same pinned versions as
# reel-bot-Carolina to inherit their tested combination.
RUN npm install -g appium@2.13.1 \
    && appium driver install --source=npm appium-uiautomator2-driver@3.10.0

# Android SDK layout that UiAutomator2 driver expects, including aapt2 (used
# to read APK version codes — without it, every UiAutomator2 session fails to
# start its instrumentation process). Cribbed from reel-bot-Carolina/Dockerfile.
ENV ANDROID_HOME=/opt/android-sdk
RUN mkdir -p ${ANDROID_HOME}/platform-tools ${ANDROID_HOME}/build-tools \
    && ln -sf /usr/bin/adb ${ANDROID_HOME}/platform-tools/adb \
    && curl -fsSL -o /tmp/build-tools.zip 'https://dl.google.com/android/repository/build-tools_r34-linux.zip' \
    && apt-get update && apt-get install -y --no-install-recommends unzip \
    && unzip -q /tmp/build-tools.zip -d /tmp/bt \
    && cp /tmp/bt/android-14/aapt2 ${ANDROID_HOME}/platform-tools/aapt2 \
    && cp -r /tmp/bt/android-14 ${ANDROID_HOME}/build-tools/34.0.0 \
    && chmod +x ${ANDROID_HOME}/platform-tools/aapt2 ${ANDROID_HOME}/build-tools/34.0.0/aapt2 \
    && rm -rf /tmp/build-tools.zip /tmp/bt /var/lib/apt/lists/*

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

# Boot script: starts Appium server in background on 127.0.0.1:4723, then
# execs the bot. The bot drives Appium in-process via Appium-Python-Client
# (only used by /ig_setup_private — everything else ignores Appium entirely).
# Pre-existing behavior — `python bot.py` — is preserved as the final exec.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'echo "[boot] starting Appium server on :4723..."' \
    'appium --base-path /wd/hub --log-level warn > /tmp/appium.log 2>&1 &' \
    'echo "[boot] starting bot..."' \
    'exec python bot.py' \
    > /app/boot.sh && chmod +x /app/boot.sh

CMD ["/app/boot.sh"]
