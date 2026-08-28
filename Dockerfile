FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 aida && useradd --uid 10001 --gid aida --no-create-home aida

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN python -m pip install --upgrade pip && python -m pip install .

USER aida
EXPOSE 8000

CMD ["uvicorn", "aida.main:app", "--host", "0.0.0.0", "--port", "8000"]

