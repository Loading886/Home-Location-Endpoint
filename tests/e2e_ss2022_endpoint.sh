#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
XRAY_BIN="$(command -v xray || true)"
[[ -n "${XRAY_BIN}" && -x "${XRAY_BIN}" ]] || {
    echo "xray is not installed" >&2
    exit 1
}

TEMPORARY="$(mktemp -d)"
SERVER_PID=""
cleanup() {
    if [[ -n "${SERVER_PID}" ]]; then
        kill "${SERVER_PID}" >/dev/null 2>&1 || true
        wait "${SERVER_PID}" >/dev/null 2>&1 || true
    fi
    rm -rf -- "${TEMPORARY}"
}
trap cleanup EXIT

read -r SERVER_PORT SOCKS_PORT < <(python3 <<'PY'
import socket

sockets = []
ports = []
try:
    for _ in range(2):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        sockets.append(listener)
        ports.append(listener.getsockname()[1])
finally:
    for listener in sockets:
        listener.close()
print(*ports)
PY
)
PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"

PYTHONPATH="${ROOT}/src" python3 -m home_location_endpoint.render \
    --config "${TEMPORARY}/server.json" \
    --uri "${TEMPORARY}/node-uri.txt" \
    --protocol ss2022 \
    --server 127.0.0.1 \
    --port "${SERVER_PORT}" \
    --listen 0.0.0.0 \
    --ss-password "${PASSWORD}" >/dev/null

"${XRAY_BIN}" run -test -config "${TEMPORARY}/server.json" >/dev/null
"${XRAY_BIN}" run -config "${TEMPORARY}/server.json" \
    >"${TEMPORARY}/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
    if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
        cat "${TEMPORARY}/server.log" >&2
        echo "temporary SS2022 server exited early" >&2
        exit 1
    fi
    if python3 - "${SERVER_PORT}" <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.2):
        pass
except OSError:
    raise SystemExit(1)
PY
    then
        break
    fi
    sleep 0.2
done

bash "${ROOT}/tests/e2e_installed_endpoint.sh" \
    --uri-file "${TEMPORARY}/node-uri.txt" \
    --connect-address 127.0.0.1 \
    --socks-port "${SOCKS_PORT}"
