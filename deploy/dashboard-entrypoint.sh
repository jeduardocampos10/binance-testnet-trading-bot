#!/bin/sh
set -eu
umask 077

if [ ! -f /data/config.json ]; then
    cp /app/config.example.json /data/config.json
fi

exec python -m app.dashboard \
    --config /data/config.json \
    --db /data/paper-crypto-only-v05.sqlite3 \
    --auth-token-file /run/secrets/dashboard_auth_token \
    --host 0.0.0.0 \
    --port 8080
