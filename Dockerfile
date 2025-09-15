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

# Runtime stage - Use distroless Python image for maximum security
FROM gcr.io/distroless/python3-debian12

# Copy virtual environment and application
COPY --from=builder /venv /venv
COPY --from=builder /app/bazarr-auto-translate.py /app/

# Set working directory and environment
WORKDIR /app
ENV PATH="/venv/bin:$PATH"

# Set default environment variables
ENV BAZARR_HOSTNAME=localhost \
    BAZARR_PORT=6767 \
    BAZARR_APIKEY=<bazarr-api-key> \
    CRON_SCHEDULE="0 6 * * *" \
    FIRST_LANG=pl

# Run application
ENTRYPOINT ["/venv/bin/python", "-u", "bazarr-auto-translate.py"]