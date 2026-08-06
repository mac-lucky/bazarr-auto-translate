# One literal decides the interpreter for both stages. It stays spelled out
# rather than coming from an ARG because the shared CI workflow greps the FROM
# lines for a version and would otherwise read back the unexpanded variable.
FROM python:3.14-alpine@sha256:26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base

# Build stage - Alpine with uv for fast dependency installation
FROM base AS builder

# Pinned: a floating tag here would change the resolver between builds.
COPY --from=ghcr.io/astral-sh/uv:0.12.2@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /bin/uv

# Install build dependencies
RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev

WORKDIR /app

# uv.lock, not pyproject.toml: installing from the loose ranges in pyproject
# resolves whatever is newest at build time, so the image would not contain the
# versions CI tested against with --locked.
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# --no-install-project skips building the script's own wheel, which is what
# makes it safe to copy only the two files above.
RUN uv sync --locked --no-dev --no-install-project

# Runtime stage
FROM base

WORKDIR /app

# Copy virtual environment and application from builder
COPY --from=builder /venv /venv
COPY bazarr-auto-translate.py /app/

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
