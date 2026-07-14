#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image_name=${FRONTEND_TEST_IMAGE:-travel-assistant-frontend-entrypoint-test-$$}
image_built=0
container_id=
headers_file=
api_url='https://api.example.test/v1?query="hello world"&next=\beijing'
api_timeout_ms='450001'

cleanup() {
  if [ -n "$container_id" ]; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  if [ -n "$headers_file" ]; then
    rm -f "$headers_file"
  fi
  if [ "$image_built" -eq 1 ]; then
    docker image rm -f "$image_name" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

test -f "$root_dir/public/config.js"
test -f "$root_dir/docker-entrypoint.sh"

docker build -t "$image_name" "$root_dir"
image_built=1
container_id=$(docker run -d --rm -P \
  -e "VITE_API_BASE_URL=$api_url" \
  -e "VITE_API_TIMEOUT_MS=$api_timeout_ms" \
  "$image_name")

host_port=$(docker port "$container_id" 80/tcp | sed -n 's/.*://p' | head -n 1)
test -n "$host_port"

config_js=
headers_file=$(mktemp)
attempt=0
while [ "$attempt" -lt 30 ]; do
  if config_js=$(curl --fail --silent --show-error -D "$headers_file" "http://127.0.0.1:$host_port/config.js"); then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
test -n "$config_js"
tr -d '\r' < "$headers_file" | grep -Eiq '^Cache-Control:.*no-store.*no-cache'

CONFIG_JS="$config_js" EXPECTED_API_URL="$api_url" EXPECTED_TIMEOUT="$api_timeout_ms" node <<'NODE'
const source = process.env.CONFIG_JS || ''
const prefix = 'window.__APP_CONFIG__ = '
if (!source.startsWith(prefix) || !source.endsWith(';')) {
  throw new Error('config.js assignment wrapper is invalid')
}
const config = JSON.parse(source.slice(prefix.length, -1))
if (config.API_BASE_URL !== process.env.EXPECTED_API_URL) {
  throw new Error('API_BASE_URL was not JSON escaped safely')
}
if (config.API_TIMEOUT_MS !== process.env.EXPECTED_TIMEOUT) {
  throw new Error('API_TIMEOUT_MS does not match the runtime environment')
}
NODE
