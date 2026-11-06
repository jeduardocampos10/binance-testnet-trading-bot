#!/bin/sh
set -eu
umask 077

if [ ! -f /data/config.json ]; then
    cp /app/config.example.json /data/config.json
fi

set -- \
    --config /data/config.json \
    --db /data/paper-crypto-only-v05.sqlite3 \
    --auth-token-file /run/secrets/dashboard_auth_token \
    --host 0.0.0.0 \
    --port 8080

# LLM support is optional, but the pair is atomic and validated by the app.
if [ -f /run/secrets/openai_api_key ] || [ -f /run/secrets/openai_model ]; then
    if [ ! -f /run/secrets/openai_api_key ] || [ ! -f /run/secrets/openai_model ]; then
        echo "both OpenAI secret files are required when LLM support is enabled" >&2
        exit 1
    fi
    set -- "$@" \
        --openai-api-key-file /run/secrets/openai_api_key \
        --openai-model-file /run/secrets/openai_model
fi

exec python -m app.dashboard "$@"
