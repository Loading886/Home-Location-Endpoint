#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="v26.3.27"
ASSET="Xray-linux-64.zip"
SHA256="23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
TEMPORARY="$(mktemp -d)"
trap 'rm -rf "${TEMPORARY}"' EXIT

curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://github.com/XTLS/Xray-core/releases/download/${VERSION}/${ASSET}" \
    -o "${TEMPORARY}/xray.zip"
printf '%s  %s\n' "${SHA256}" "${TEMPORARY}/xray.zip" | sha256sum --check --status
unzip -q "${TEMPORARY}/xray.zip" xray -d "${TEMPORARY}"
chmod 0755 "${TEMPORARY}/xray"

CLIENT_UUID="$("${TEMPORARY}/xray" uuid)"
KEY_OUTPUT="$("${TEMPORARY}/xray" x25519)"
PRIVATE_KEY="$(printf '%s\n' "${KEY_OUTPUT}" | awk -F': *' '/^(PrivateKey|Private key):/{print $2; exit}')"
PUBLIC_KEY="$(printf '%s\n' "${KEY_OUTPUT}" | awk -F': *' '/^(Password \(PublicKey\)|Password|PublicKey|Public key):/{print $2; exit}')"
[[ -n "${PRIVATE_KEY}" && -n "${PUBLIC_KEY}" ]]
printf '\x30\x00' > "${TEMPORARY}/ca.der"

PYTHONPATH=src python3 -m home_location_endpoint.render \
    --config "${TEMPORARY}/config.json" \
    --uri "${TEMPORARY}/node-uri.txt" \
    --profile "${TEMPORARY}/profile.mobileconfig" \
    --ca-der "${TEMPORARY}/ca.der" \
    --server 203.0.113.9 --port 443 --uuid "${CLIENT_UUID}" \
    --reality-sni www.usc.edu --reality-target www.usc.edu:443 \
    --private-key="${PRIVATE_KEY}" --public-key="${PUBLIC_KEY}" \
    --short-id 0123456789abcdef \
    --listen :: \
    --fallback-upload-after 8388608 \
    --fallback-upload-rate 786432 \
    --fallback-upload-burst 3145728 \
    --fallback-download-after 9437184 \
    --fallback-download-rate 917504 \
    --fallback-download-burst 4194304

"${TEMPORARY}/xray" run -test -config "${TEMPORARY}/config.json"
grep -q 'packetEncoding=xudp' "${TEMPORARY}/node-uri.txt"
