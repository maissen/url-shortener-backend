# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Upgrade pip + setuptools with pinned versions for reproducibility
# (fixes CVEs and satisfies hadolint DL3013)
RUN pip install --no-cache-dir --upgrade \
    pip==25.0.1 \
    setuptools==80.0.0

# Install dependencies into an isolated prefix
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Apply OS security patches
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Remove build-time tools (optional hardening)
RUN pip uninstall -y pip setuptools wheel 2>/dev/null || true

# Copy application code
COPY app.py .

# Switch to non-root user
USER appuser

# Expose application port
EXPOSE 3000

# Run production server
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--threads", "2", "app:app"]