#!/usr/bin/env bash
set -Eeuo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

EXPECTED_EGRESS=""
SOCKS_PORT="19081"
URI_FILE=""
CONNECT_ADDRESS=""
TEST_UDP=0

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
        --test-udp)
            TEST_UDP=1
            shift
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
    HLE_TEST_PROXY_URI="$(<"${URI_FILE}")"
else
    [[ "${EUID}" -eq 0 ]] || { echo "run as root when reading the installed endpoint URI" >&2; exit 1; }
    [[ -x /usr/local/sbin/hle ]] || { echo "hle is not installed; provide --uri-file" >&2; exit 1; }
    HLE_TEST_PROXY_URI="$(hle show-link)"
    CONNECT_ADDRESS="${CONNECT_ADDRESS:-127.0.0.1}"
fi
export HLE_TEST_PROXY_URI SOCKS_PORT CONNECT_ADDRESS

python3 >"${TEMPORARY}/client.json" <<'PY'
import base64
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

uri = urlsplit(os.environ["HLE_TEST_PROXY_URI"])
connect_address = os.environ.get("CONNECT_ADDRESS") or uri.hostname

if uri.scheme == "vless":
    query = {key: values[-1] for key, values in parse_qs(uri.query).items()}
    required = ("flow", "sni", "fp", "pbk", "sid", "packetEncoding")
    missing = [key for key in required if not query.get(key)]
    if missing:
        raise SystemExit("VLESS URI is missing: " + ", ".join(missing))
    outbound = {
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
    }
elif uri.scheme == "ss":
    encoded = uri.username or ""
    try:
        decoded = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("ascii")
        method, password = decoded.split(":", 1)
    except (ValueError, UnicodeError) as exc:
        raise SystemExit("invalid SIP002 SS URI") from exc
    if method != "2022-blake3-aes-256-gcm":
        raise SystemExit("unexpected Shadowsocks method: " + method)
    outbound = {
        "protocol": "shadowsocks",
        "settings": {"servers": [{
            "address": connect_address,
            "port": uri.port,
            "method": method,
            "password": password,
        }]},
    }
else:
    raise SystemExit("unsupported proxy URI scheme: " + uri.scheme)

config = {
    "log": {"loglevel": "info"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": int(os.environ["SOCKS_PORT"]),
        "protocol": "socks",
        "settings": {"udp": True},
    }],
    "outbounds": [outbound],
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

if [[ "${TEST_UDP}" -eq 1 ]]; then
    python3 - "${SOCKS_PORT}" <<'PY'
import secrets
import socket
import struct
import sys


def receive_exact(connection, size):
    result = b""
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise RuntimeError("SOCKS control connection closed early")
        result += block
    return result


def receive_address(connection, atyp):
    if atyp == 1:
        host = socket.inet_ntoa(receive_exact(connection, 4))
    elif atyp == 3:
        length = receive_exact(connection, 1)[0]
        host = receive_exact(connection, length).decode("ascii")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, receive_exact(connection, 16))
    else:
        raise RuntimeError("SOCKS returned an invalid address type")
    port = struct.unpack("!H", receive_exact(connection, 2))[0]
    return host, port


control = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
control.settimeout(5)
control.sendall(b"\x05\x01\x00")
if receive_exact(control, 2) != b"\x05\x00":
    raise SystemExit("SOCKS server rejected no-auth negotiation")
control.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
header = receive_exact(control, 4)
if header[:2] != b"\x05\x00":
    raise SystemExit("SOCKS UDP ASSOCIATE failed")
relay_host, relay_port = receive_address(control, header[3])
if relay_host in {"0.0.0.0", "::"}:
    relay_host = "127.0.0.1"

transaction = secrets.token_bytes(2)
query = transaction + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
for label in (b"example", b"com"):
    query += bytes([len(label)]) + label
query += b"\x00\x00\x01\x00\x01"
socks_packet = (
    b"\x00\x00\x00\x01" + socket.inet_aton("1.1.1.1")
    + struct.pack("!H", 53) + query
)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.settimeout(10)
udp.sendto(socks_packet, (relay_host, relay_port))
response, _peer = udp.recvfrom(65535)
if len(response) < 10 or response[:3] != b"\x00\x00\x00":
    raise SystemExit("invalid SOCKS UDP response")
atyp = response[3]
offset = 4 + ({1: 4, 4: 16}.get(atyp, 0))
if atyp == 3:
    offset = 5 + response[4]
if atyp not in {1, 3, 4} or len(response) < offset + 4:
    raise SystemExit("invalid SOCKS UDP response address")
dns_response = response[offset + 2:]
if len(dns_response) < 12 or dns_response[:2] != transaction:
    raise SystemExit("DNS response transaction mismatch")
print("SOCKS5 UDP relay: OK")
PY
fi

printf 'Proxy endpoint end-to-end: OK (%s)\n' "${HLE_TEST_PROXY_URI%%:*}"
printf 'Observed egress IP: %s\n' "${EGRESS_IP}"
printf 'Cloudflare connectivity: HTTP %s\n' "${HTTP_CODE}"
