FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_FROZEN=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN groupadd --gid 10001 aida && useradd --uid 10001 --gid aida --no-create-home aida

# uv's static binary. AU-13: install from the same uv.lock that CI's `quality`/
# `tests` jobs resolve and test against, not a fresh `pip install .` resolve --
# so image dependency versions can never float away from what CI validated.
RUN python -m pip install --upgrade pip && python -m pip install uv==0.8.17

COPY pyproject.toml uv.lock alembic.ini ./
COPY src ./src
COPY migrations ./migrations

# --frozen (also set via UV_FROZEN above): fail the build rather than silently
# re-resolving a dependency set that differs from the committed lockfile, same
# contract ci.yml's env block documents for every CI job. --no-dev: the runtime
# image excludes lint/test/dev-only extras.
RUN uv sync --frozen --no-dev

USER aida
EXPOSE 8000

CMD ["uvicorn", "aida.main:app", "--host", "0.0.0.0", "--port", "8000"]

