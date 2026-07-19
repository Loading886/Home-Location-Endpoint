#!/usr/bin/env bash
set -Eeuo pipefail

PROFILE=/etc/home-location-endpoint/Home-Location-Endpoint-CA.mobileconfig
TMPDIR_TEST="$(mktemp -d)"
SERVER_PID=""

cleanup() {
    if [[ -n "${SERVER_PID}" ]]; then
        kill "${SERVER_PID}" >/dev/null 2>&1 || true
        wait "${SERVER_PID}" >/dev/null 2>&1 || true
    fi
    rm -rf -- "${TMPDIR_TEST}"
}
trap cleanup EXIT

[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -s "${PROFILE}" ]] || { echo "CA profile is missing" >&2; exit 1; }
command -v hle >/dev/null 2>&1 || { echo "hle is not installed" >&2; exit 1; }

PYTHONUNBUFFERED=1 hle profile serve \
    --bind 127.0.0.1 \
    --host 127.0.0.1 \
    --port 0 \
    --timeout-minutes 1 \
    --no-qr >"${TMPDIR_TEST}/server.log" 2>&1 &
SERVER_PID=$!

URL=""
for _ in $(seq 1 100); do
    if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
        cat "${TMPDIR_TEST}/server.log" >&2
        echo "profile server exited before publishing a URL" >&2
        exit 1
    fi
    URL="$(grep -Eo 'http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9_-]+/Home-Location-Endpoint-CA\.mobileconfig' \
        "${TMPDIR_TEST}/server.log" | head -n 1 || true)"
    [[ -z "${URL}" ]] || break
    sleep 0.05
done
[[ -n "${URL}" ]] || {
    cat "${TMPDIR_TEST}/server.log" >&2
    echo "profile server did not publish a usable URL" >&2
    exit 1
}

curl -fsS -D "${TMPDIR_TEST}/headers" "${URL}" \
    -o "${TMPDIR_TEST}/download.mobileconfig"
cmp -- "${PROFILE}" "${TMPDIR_TEST}/download.mobileconfig"
grep -Eiq '^Content-Type:[[:space:]]*application/x-apple-aspen-config([[:space:]]*;.*)?[[:space:]]*$' \
    "${TMPDIR_TEST}/headers"

for _ in $(seq 1 100); do
    if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
        wait "${SERVER_PID}"
        SERVER_PID=""
        break
    fi
    sleep 0.05
done
[[ -z "${SERVER_PID}" ]] || {
    echo "profile server did not stop after the first successful download" >&2
    exit 1
}

if curl -fsS --max-time 1 "${URL}" >/dev/null 2>&1; then
    echo "one-time profile URL remained reachable after download" >&2
    exit 1
fi

printf 'One-time CA profile handoff: OK\n'
