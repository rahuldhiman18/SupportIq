# ---- Stage 1: dependencies (cached unless requirements.txt changes) --------
# slim = ~150MB, has no compilers -- psycopg ships manylinux wheels, so no
# build toolchain needed. Layer-cached: pip install only reruns when the
# requirements file itself changes, not on every code edit.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# Re-encoded to UTF-8 (was UTF-16) -- pip's clean-build path needs it.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: application code ---------------------------------------------
# deps layer stays cached; only code edits rerun from this COPY down.
# NOTE: a directory source like `alembic/` copies its CONTENTS, not the folder
# name, so each dir needs an explicit destination (alembic/ -> ./alembic/).
# Getting this wrong flattens alembic/{env.py,versions} into the app root and
# `alembic upgrade head` then fails at container start with "no ./alembic".
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/

# Non-root: a container escape as root owns the host; as appuser owns nothing.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

# HEALTHCHECK = the /healthz we built (open, 3 strings, DB ping -> 503).
# interval 30s | timeout 5s | start_period gives alembic time on first boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# Migrate once, then serve. Inline mode is the compose default (queued flips
# on as the second smoke). 0.0.0.0 so the port is reachable from outside the
# container; --workers=1 keeps the single in-process queue worker (matches the
# heartbeat/throttle design).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]