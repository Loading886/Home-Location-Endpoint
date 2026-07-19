#!/usr/bin/env bash
set -Eeuo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

EXPECTED_EGRESS=""
SOCKS_PORT="19081"
URI_FILE=""
CONNECT_ADDRESS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --expected-egress)
            [[ $# -ge 2 ]] || { echo "missing value for --expected-egress" >&2; exit 2; }
            EXPECTED_EGRESS="$2"
            shift 2
            ;;
        --socks-port)
            [[ $# -ge 2 ]] || { echo "missing value for --socks-port" >&2; exit 2; }
            SOCKS_PORT="$2"
            shift 2
            ;;
        --uri-file)
            [[ $# -ge 2 ]] || { echo "missing value for --uri-file" >&2; exit 2; }
            URI_FILE="$2"
            shift 2
            ;;
        --connect-address)
            [[ $# -ge 2 ]] || { echo "missing value for --connect-address" >&2; exit 2; }
            CONNECT_ADDRESS="$2"
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

XRAY_BIN="$(command -v xray || true)"
[[ -n "${XRAY_BIN}" && -x "${XRAY_BIN}" ]] || { echo "xray is not installed" >&2; exit 1; }
command -v curl >/dev/null
command -v python3 >/dev/null

TEMPORARY="$(mktemp -d)"
XRAY_PID=""
cleanup() {
    if [[ -n "${XRAY_PID}" ]]; then
        kill "${XRAY_PID}" >/dev/null 2>&1 || true
        wait "${XRAY_PID}" >/dev/null 2>&1 || true
    fi
    rm -rf -- "${TEMPORARY}"
}
trap cleanup EXIT

if [[ -n "${URI_FILE}" ]]; then
    [[ -f "${URI_FILE}" ]] || { echo "URI file does not exist" >&2; exit 1; }
    HLE_TEST_VLESS_URI="$(<"${URI_FILE}")"
else
    [[ "${EUID}" -eq 0 ]] || { echo "run as root when reading the installed endpoint URI" >&2; exit 1; }
    [[ -x /usr/local/sbin/hle ]] || { echo "hle is not installed; provide --uri-file" >&2; exit 1; }
    HLE_TEST_VLESS_URI="$(hle show-link)"
    CONNECT_ADDRESS="${CONNECT_ADDRESS:-127.0.0.1}"
fi
export HLE_TEST_VLESS_URI SOCKS_PORT CONNECT_ADDRESS

python3 >"${TEMPORARY}/client.json" <<'PY'
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

uri = urlsplit(os.environ["HLE_TEST_VLESS_URI"])
query = {key: values[-1] for key, values in parse_qs(uri.query).items()}
connect_address = os.environ.get("CONNECT_ADDRESS") or uri.hostname
required = ("flow", "sni", "fp", "pbk", "sid", "packetEncoding")
missing = [key for key in required if not query.get(key)]
if missing:
    raise SystemExit("VLESS URI is missing: " + ", ".join(missing))

config = {
    "log": {"loglevel": "info"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": int(os.environ["SOCKS_PORT"]),
        "protocol": "socks",
        "settings": {"udp": True},
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {"vnext": [{
            "address": connect_address,
            "port": uri.port,
            "users": [{
                "id": uri.username,
                "encryption": "none",
                "flow": query["flow"],
                "packetEncoding": query["packetEncoding"],
            }],
        }]},
        "streamSettings": {
            "network": "raw",
            "security": "reality",
            "realitySettings": {
                "serverName": query["sni"],
                "fingerprint": query["fp"],
                "publicKey": query["pbk"],
                "shortId": query["sid"],
                "spiderX": "/",
            },
        },
    }],
}
json.dump(config, sys.stdout)
PY

"${XRAY_BIN}" run -test -config "${TEMPORARY}/client.json" >/dev/null
"${XRAY_BIN}" run -config "${TEMPORARY}/client.json" \
    >"${TEMPORARY}/xray.log" 2>&1 &
XRAY_PID=$!

for _ in $(seq 1 50); do
    if ! kill -0 "${XRAY_PID}" >/dev/null 2>&1; then
        cat "${TEMPORARY}/xray.log" >&2
        echo "Xray client exited before opening the SOCKS listener" >&2
        exit 1
    fi
    if python3 - "${SOCKS_PORT}" <<'PY'
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

EGRESS_IP="$(curl --fail --silent --show-error --max-time 20 \
    --socks5-hostname "127.0.0.1:${SOCKS_PORT}" https://api.ipify.org)" || {
    cat "${TEMPORARY}/xray.log" >&2
    exit 1
}
HTTP_CODE="$(curl --silent --show-error --max-time 20 --output /dev/null \
    --write-out '%{http_code}' --socks5-hostname "127.0.0.1:${SOCKS_PORT}" \
    https://cp.cloudflare.com/generate_204)"

if [[ -n "${EXPECTED_EGRESS}" && "${EGRESS_IP}" != "${EXPECTED_EGRESS}" ]]; then
    echo "unexpected egress IP: ${EGRESS_IP}" >&2
    exit 1
fi
[[ "${HTTP_CODE}" == "204" ]] || { echo "unexpected HTTP status: ${HTTP_CODE}" >&2; exit 1; }

printf 'VLESS + REALITY end-to-end: OK\n'
printf 'Observed egress IP: %s\n' "${EGRESS_IP}"
printf 'Cloudflare connectivity: HTTP %s\n' "${HTTP_CODE}"
