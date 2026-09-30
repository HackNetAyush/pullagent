# One image, two roles. `cr app serve --role web` takes webhooks; `cr app
# worker` runs reviews. Building them separately would let the thing that
# reviews and the thing that accepts work drift apart, which is the one bug
# neither container could diagnose alone.

FROM python:3.12-slim AS base

# git is not a nicety here: the reviewer clones and mirrors repositories to
# build its symbol graph, so the runtime needs a real git, not a library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Mounted from Azure Files in the deployed environment. Everything that has
    # to survive a replica — git mirrors, the symbol graph, the review cache —
    # already derives from this one variable.
    CR_CACHE_DIR=/cache \
    CR_DASHBOARD_DIST=/app/dashboard/dist

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[azure]"

# The dashboard is prebuilt in CI and copied in; building node here would put a
# toolchain in the runtime image for the sake of static files.
COPY dashboard/dist/ ./dashboard/dist/

RUN useradd --create-home --uid 10001 cr \
    && mkdir -p /cache \
    && chown -R cr:cr /cache /app
USER cr

EXPOSE 8000

# Overridden to `cr app worker` for the worker revision. The default is the
# single-container shape, which is also what runs locally.
CMD ["cr", "app", "serve", "--host", "0.0.0.0", "--port", "8000"]
