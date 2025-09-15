# Build stage - Use Alpine for smaller size and faster builds
FROM python:3.13-alpine AS builder

# Install build dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev \
    && python -m venv /venv \
    && /venv/bin/pip install --upgrade pip setuptools wheel

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bazarr-auto-translate.py .

# Runtime stage - Use Alpine for better Python package compatibility
FROM python:3.13-alpine

# Set working directory
WORKDIR /app

# Copy virtual environment and application from builder
COPY --from=builder /venv /venv
COPY --from=builder /app/bazarr-auto-translate.py /app/

# Set environment path
ENV PATH="/venv/bin:$PATH"

# Create non-root user
RUN addgroup -g 1000 appgroup && \
    adduser -u 1000 -G appgroup -s /bin/sh -D appuser && \
    chown -R appuser:appgroup /app

# Switch to non-root user
USER appuser

# Set default environment variables
ENV BAZARR_HOSTNAME=localhost \
    BAZARR_PORT=6767 \
    BAZARR_APIKEY=<bazarr-api-key> \
    CRON_SCHEDULE="0 6 * * *" \
    FIRST_LANG=pl

# Run application
ENTRYPOINT ["/venv/bin/python", "-u", "bazarr-auto-translate.py"]