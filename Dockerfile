# Build stage - Use Alpine with uv for fast dependency installation
FROM python:3.14-alpine AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install build dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY bazarr-auto-translate.py .

# Create virtual environment and install dependencies
RUN uv venv /venv && \
    uv pip install --python /venv/bin/python -r pyproject.toml

# Runtime stage - Use Alpine for smaller image
FROM python:3.14-alpine

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
