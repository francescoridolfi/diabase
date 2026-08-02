#!/bin/sh
# Boot the Diabase container: settle the secret key, migrate, serve.
set -eu

# The secret key encrypts stored LLM API keys (Fernet), so it must survive
# restarts: generate one into the data volume on first boot and reuse it.
if [ -z "${DIABASE_SECRET_KEY:-}" ]; then
    if [ ! -f /data/secret_key ]; then
        uv run python -c "import secrets; print(secrets.token_urlsafe(50))" > /data/secret_key
        chmod 600 /data/secret_key
    fi
    DIABASE_SECRET_KEY="$(cat /data/secret_key)"
    export DIABASE_SECRET_KEY
fi

uv run python manage.py migrate --noinput

# one worker: turn/reindex threads and their locks are per-process; many
# threads because every open SSE stream holds one for its whole duration
exec uv run gunicorn diabase.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${DIABASE_WORKERS:-1}" \
    --threads "${DIABASE_THREADS:-16}" \
    --timeout "${DIABASE_TIMEOUT:-120}"
