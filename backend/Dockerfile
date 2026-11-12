# Agent-X API image (Phase 8). Multi-stage: dependencies are built as wheels in a builder stage and installed into a
# slim runtime that has no compiler, runs as a fixed non-root UID, and carries no secret (everything comes from the
# environment at run time). Migrations are NOT run on start-up: they are an explicit, backed-up operator step
# (`docker compose run --rm migrate`, see docs/DEPLOYMENT.md).

FROM python:3.14-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# PostgreSQL 18 client tools for `python -m app.ops.cli backup|verify-backup|restore-check` (no server). They must match
# the server's major version (pg_dump refuses newer servers), so they come from the PostgreSQL project's apt repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt && rm -rf /wheels /tmp/requirements.txt

WORKDIR /app
COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

RUN groupadd --system --gid 10001 agentx \
    && useradd --system --uid 10001 --gid agentx --no-create-home --shell /usr/sbin/nologin agentx
USER 10001:10001

EXPOSE 8000
# Readiness = database reachable and schema at this image's migration head.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly; in-flight requests get up to 20 s to finish.
# Forwarded headers are trusted only from TRUSTED_PROXY_IPS (the reverse proxy), never from arbitrary clients.
CMD ["sh", "-c", "exec uvicorn --factory app.main:build_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\" --timeout-graceful-shutdown 20 --no-server-header"]
