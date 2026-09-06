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
# REVIEW.md §7 packaging gap: `[tool.hatch.build.targets.wheel].packages` in
# pyproject.toml declares three package roots -- `src/aida`, `src/atlas` and
# `sdk/aida_tool_sdk` -- but only `src` was copied here. That did not fail the
# build: hatchling's editable install wrote a .pth for the roots it could see
# and silently skipped the missing one, so the image came out claiming to be an
# install of this project while `import aida_tool_sdk` raised ModuleNotFoundError.
# The decision recorded in Docs/60-delivery/20-capability-register.md is that the
# image IS the project's declared distribution, so it ships every declared
# package. `scripts/check_image_packaging.py` fails CI if a package root is
# added to the manifest without a matching COPY here (and the docker-build job
# imports the SDK from the built image as the runtime proof).
COPY sdk ./sdk

# --frozen (also set via UV_FROZEN above): fail the build rather than silently
# re-resolving a dependency set that differs from the committed lockfile, same
# contract ci.yml's env block documents for every CI job. --no-dev: the runtime
# image excludes lint/test/dev-only extras.
RUN uv sync --frozen --no-dev

USER aida
EXPOSE 8000

CMD ["uvicorn", "aida.main:app", "--host", "0.0.0.0", "--port", "8000"]

