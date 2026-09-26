# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ----------------------------------------------------------------- run stage
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# Non-root: the API never needs write access to the image.
RUN groupadd --gid 10001 jobscout \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin jobscout

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY scripts ./scripts

RUN chown -R jobscout:jobscout /app
USER jobscout

EXPOSE 8000 8501

# Default: the query API. `docker compose` overrides this per service.
CMD ["python", "-m", "uvicorn", "jobscout.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
