# One literal decides the interpreter for both stages. It stays spelled out
# rather than coming from an ARG because the shared CI workflow greps the FROM
# lines for a version and would otherwise read back the unexpanded variable.
FROM python:3.14-alpine@sha256:3f818d6811ff5f3f2b5e5d836df3d25c2dd2e588d3b4981338a8ba17e422f74f AS base

# Build stage - Alpine with uv for fast dependency installation
FROM base AS builder

# Pinned: a floating tag here would change the resolver between builds.
COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /bin/uv

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

# apk upgrade first: the digest-pinned base can lag Alpine package fixes, and
# upgrading at build picks them up without waiting for a base-image rebuild.
RUN apk upgrade --no-cache

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
