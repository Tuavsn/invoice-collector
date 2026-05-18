FROM python:3.11-slim-bookworm

# ── System dependencies ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright system dependencies
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libpango-1.0-0 \
    libxss1 \
    # Build utils
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ──────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright browsers ──────────────────────────────────────────
RUN playwright install chromium --with-deps

# ── Application code ─────────────────────────────────────────────
COPY . .

# ── Runtime directories ──────────────────────────────────────────
RUN mkdir -p downloads invoices exports logs/screenshots

# ── Environment defaults ─────────────────────────────────────────
ENV FLASK_ENV=production \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5000 \
    PLAYWRIGHT_HEADLESS=true \
    DATABASE_URL=sqlite:///invoices.db

EXPOSE 5000

CMD ["python", "run.py"]