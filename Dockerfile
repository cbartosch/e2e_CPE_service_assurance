# The image `docker-compose.yml` builds. One stage, because there is nothing to compile.
#
# **This build has never succeeded on the machine it was written on, and the reason is the network
# rather than this file.** `docker compose config` validates, and `docker compose build` fails at
# `pip install` with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` against
# `pypi.org` -- a TLS-inspecting corporate proxy whose root CA the container does not trust. Adding
# `--trusted-host pypi.org` would make the build pass here and would ship an image that installs
# dependencies without verifying them, which is a worse thing to have than an unverified Dockerfile.
# The CI `compose` job is where this is first proven; until it has run, treat this file as reviewed
# and not as tested. Gap DOCKER-1.
#
# `psycopg[binary]` is the reason the postgres extra is installed here rather than left optional.
# IMPLEMENTATION_PLAN.md §2 records the finding: importing `langgraph.checkpoint.postgres` raises
# `ImportError: no pq wrapper available` unless libpq is present, and the bare `psycopg` wheel is not
# enough. An image that omitted it would run fine until the first checkpoint and then fail on an
# import the code is written to survive missing -- which is the worst of both.

FROM python:3.12-slim

# `PYTHONDONTWRITEBYTECODE` because the source is read-only in the container and `.pyc` files would
# be written to a layer nobody reads. `PYTHONUNBUFFERED` so structured logs reach the log driver in
# order rather than in 4KB blocks.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The metadata first, so a source-only change does not reinstall dependencies. `README.md` is copied
# with it because `pyproject.toml` names it as the long description and the build fails without it.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[postgres]"

# A non-root user, and the source owned by it. Not defence in depth for a development stack so much
# as the shape that does not have to be undone later: a container that has only ever run as root
# acquires code that assumes it can write anywhere.
RUN useradd --create-home --uid 10001 lpr && chown -R lpr:lpr /app
USER lpr

EXPOSE 8000

# No CMD. `docker-compose.yml` supplies the command, and an image whose default was `uvicorn` would
# invite `docker run` against a stack with no database, which selects the in-memory checkpointer and
# silently loses every approval on restart. Being explicit at the compose layer keeps that choice
# visible where the DSN is set.
