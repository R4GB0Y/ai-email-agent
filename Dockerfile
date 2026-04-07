# ---------- Stage 1: Build ----------
# WHY multi-stage? Your final image doesn't need gcc, pip cache, etc.
# Smaller image = faster deploys = lower costs.

FROM python:3.11-slim AS builder

WORKDIR /app

# Copy requirements FIRST (before code)
# WHY? Docker caches layers. If requirements.txt hasn't changed,
# Docker reuses the cached pip install. Saves minutes on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/app/deps -r requirements.txt

# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /app/deps /app/deps

# Set Python path to find our installed packages
ENV PYTHONPATH="/app/deps:/app"
# Don't buffer output — see logs in real time
ENV PYTHONUNBUFFERED=1

# Copy application code
COPY src/ ./src/
COPY config/ ./config/

# Health check — so orchestrators know if we're alive
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "print('healthy')" || exit 1

# Run the agent
CMD ["python", "-m", "src.agent"]

