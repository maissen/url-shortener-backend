# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install dependencies into an isolated prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py .

# Drop to non-root
USER appuser

# Document the port (matches the PORT env var default)
EXPOSE 3000
 
# Use gunicorn for production — workers/threads tunable via env vars
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--threads", "2", "app:app"]
