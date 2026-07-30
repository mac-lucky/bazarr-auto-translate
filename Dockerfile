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

# Create non-root user. /state has to exist in the image and be owned by that
# user, otherwise a named volume mounted there is created root-owned and the
# daemon cannot record which items it gave up on.
RUN addgroup -g 1000 appgroup && \
    adduser -u 1000 -G appgroup -s /bin/sh -D appuser && \
    mkdir -p /state && \
    chown -R appuser:appgroup /app /state

# Switch to non-root user
USER 1000

# Defaults live in the script itself. Repeating them here only shadowed them
# from a second place, and shipped a placeholder that was sent as a real
# X-API-KEY header. See the README for the full list.

# Run application
ENTRYPOINT ["/venv/bin/python", "-u", "bazarr-auto-translate.py"]
