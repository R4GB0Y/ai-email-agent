# ─────────────────────────────────────────────
# Stage 1: builder
# Install dependencies in an isolated layer so changes to src/ don't invalidate the dep cache.
# ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app


COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder (avoids reinstalling in runtime layer)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy source code
COPY src/       ./src/
COPY config/    ./config/

# Removed this copy tests file because in prod good practices the tests are prohibited by .dockerignore file, because tests are not needed in production.
# COPY tests/     ./tests/

# Non-root user — production best practice
RUN useradd --create-home appuser \
 && chown -R appuser:appuser /app
USER appuser

# Default: run the pipeline scheduler.
# Override with RUN_MODE=server to start the HTTP API instead.
ENV RUN_MODE=pipeline
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", \
     "if [ \"$RUN_MODE\" = 'server' ]; then python -m src.server; \
      else python -m src.pipeline; fi"]