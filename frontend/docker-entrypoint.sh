#!/bin/sh
set -eu
target=/usr/share/nginx/html/config.js
printf 'window.__APP_CONFIG__ = ' > "$target"
jq -cn \
  --arg apiBaseUrl "${VITE_API_BASE_URL:-}" \
  --arg apiTimeoutMs "${VITE_API_TIMEOUT_MS:-300000}" \
  '{API_BASE_URL:$apiBaseUrl,API_TIMEOUT_MS:$apiTimeoutMs}' >> "$target"
printf ';\n' >> "$target"
exec "$@"
