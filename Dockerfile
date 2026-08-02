FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# dependencies first so code edits don't bust the layer cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && DIABASE_SECRET_KEY=collectstatic-only uv run python manage.py collectstatic --noinput

RUN useradd --create-home diabase \
    && mkdir /data \
    && chown -R diabase:diabase /data /app
USER diabase

VOLUME /data
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
