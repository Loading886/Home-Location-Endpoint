#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="Home-Location-Endpoint"
REPOSITORY="https://github.com/Loading886/Home-Location-Endpoint"
BOOTSTRAP_VERSION="${HLE_VERSION:-main}"
XRAY_VERSION="v26.3.27"
XRAY_AMD64_SHA256="23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
XRAY_ARM64_SHA256="4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c"

ETC_DIR="/etc/home-location-endpoint"
APP_DIR="/opt/home-location-endpoint"
STATE_DIR="/var/lib/home-location-endpoint"
LOG_DIR="/var/log/home-location-endpoint"
XRAY_CONFIG_DIR="/usr/local/etc/xray"
MARKER="${ETC_DIR}/managed-by-installer"

PORT="443"
SERVER=""
REALITY_SNI="www.microsoft.com"
REALITY_TARGET=""
ROTATE_CA=0
XRAY_BACKUP=""

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\n==> %s\n' "$*"
}

cleanup() {
    if [[ -n "${XRAY_BACKUP}" && -f "${XRAY_BACKUP}" ]]; then
        rm -f "${XRAY_BACKUP}"
    fi
}

trap cleanup EXIT

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run this installer as root"
}

bootstrap_if_needed() {
    local script_dir archive_url temporary extracted
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
    if [[ -f "${script_dir}/src/home_location_endpoint/interceptor.py" ]]; then
        SOURCE_DIR="${script_dir}"
        return
    fi

    note "Downloading ${PROJECT} ${BOOTSTRAP_VERSION}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl tar
    temporary="$(mktemp -d)"
    if [[ "${BOOTSTRAP_VERSION}" == "main" ]]; then
        archive_url="${REPOSITORY}/archive/refs/heads/main.tar.gz"
    else
        archive_url="${REPOSITORY}/archive/refs/tags/${BOOTSTRAP_VERSION}.tar.gz"
    fi
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        "${archive_url}" -o "${temporary}/source.tar.gz"
    tar -xzf "${temporary}/source.tar.gz" -C "${temporary}"
    extracted="$(find "${temporary}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    [[ -n "${extracted}" ]] || die "downloaded archive did not contain a source directory"
    exec bash "${extracted}/install.sh" "$@"
}

load_existing_settings() {
    if [[ -f "${ETC_DIR}/install.env" ]]; then
        # This file is created root-owned and mode 0600 by this installer.
        # shellcheck disable=SC1091
        source "${ETC_DIR}/install.env"
        PORT="${HLE_PORT:-${PORT}}"
        SERVER="${HLE_SERVER:-${SERVER}}"
        REALITY_SNI="${HLE_REALITY_SNI:-${REALITY_SNI}}"
        REALITY_TARGET="${HLE_REALITY_TARGET:-${REALITY_TARGET}}"
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port)
                [[ $# -ge 2 ]] || die "--port needs a value"
                PORT="$2"
                shift 2
                ;;
            --server)
                [[ $# -ge 2 ]] || die "--server needs a value"
                SERVER="$2"
                shift 2
                ;;
            --reality-sni)
                [[ $# -ge 2 ]] || die "--reality-sni needs a value"
                REALITY_SNI="$2"
                REALITY_TARGET=""
                shift 2
                ;;
            --reality-target)
                [[ $# -ge 2 ]] || die "--reality-target needs a value"
                REALITY_TARGET="$2"
                shift 2
                ;;
            --rotate-ca)
                ROTATE_CA=1
                shift
                ;;
            -h|--help)
                cat <<'EOF'
Usage: sudo bash install.sh [options]

  --port PORT              VLESS + REALITY listening port (default: 443)
  --server HOST_OR_IP      address written into the client URI (default: detected egress IP)
  --reality-sni HOST       REALITY SNI (default: www.microsoft.com)
  --reality-target H:P     REALITY camouflage target (default: SNI:443)
  --rotate-ca              replace the scoped CA and leaf certificate

The installer never changes SSH ports, SSH keys, passwords, or existing firewall policy.
EOF
                exit 0
                ;;
            *)
                die "unknown option: $1"
                ;;
        esac
    done
    [[ "${PORT}" =~ ^[0-9]+$ ]] || die "port must be numeric"
    (( PORT >= 1 && PORT <= 65535 )) || die "port must be between 1 and 65535"
    [[ -n "${REALITY_TARGET}" ]] || REALITY_TARGET="${REALITY_SNI}:443"
}

check_os() {
    [[ -r /etc/os-release ]] || die "/etc/os-release is missing"
    # shellcheck disable=SC1091
    source /etc/os-release
    command -v apt-get >/dev/null 2>&1 || die "apt-get is required"
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    [[ -d /run/systemd/system ]] || die "systemd must be running as PID 1"
    case "${ID}:${VERSION_ID}" in
        debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) ;;
        *) die "supported systems: Debian 12/13 and Ubuntu 22.04/24.04; found ${ID}:${VERSION_ID}" ;;
    esac
    case "$(dpkg --print-architecture)" in
        amd64)
            XRAY_ASSET="Xray-linux-64.zip"
            XRAY_SHA256="${XRAY_AMD64_SHA256}"
            ;;
        arm64)
            XRAY_ASSET="Xray-linux-arm64-v8a.zip"
            XRAY_SHA256="${XRAY_ARM64_SHA256}"
            ;;
        *) die "supported CPU architectures: amd64 and arm64" ;;
    esac
}

check_port_available() {
    local active_port=""
    if [[ -f "${MARKER}" && -f "${XRAY_CONFIG_DIR}/config.json" ]] \
        && systemctl is-active --quiet xray.service; then
        active_port="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["inbounds"][0]["port"])' "${XRAY_CONFIG_DIR}/config.json" 2>/dev/null || true)"
        if [[ "${active_port}" == "${PORT}" ]]; then
            return
        fi
    fi
    python3 - "${PORT}" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
except OSError as exc:
    raise SystemExit("TCP port %d is unavailable: %s" % (port, exc))
finally:
    sock.close()
PY
}

check_existing_installation() {
    if [[ ! -f "${MARKER}" ]] && {
        [[ -f "${XRAY_CONFIG_DIR}/config.json" ]] ||
        [[ -f /etc/systemd/system/xray.service ]] ||
        [[ -f /lib/systemd/system/xray.service ]];
    }; then
        die "an unmanaged Xray installation already exists; use a clean landing server"
    fi
}

install_packages() {
    note "Installing required packages"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        ca-certificates curl iproute2 kmod logrotate openssl procps \
        python3 unzip uuid-runtime
}

install_xray() {
    local temporary archive
    note "Installing verified Xray ${XRAY_VERSION}"
    temporary="$(mktemp -d)"
    archive="${temporary}/${XRAY_ASSET}"
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${XRAY_ASSET}" \
        -o "${archive}"
    printf '%s  %s\n' "${XRAY_SHA256}" "${archive}" | sha256sum --check --status \
        || die "Xray archive checksum mismatch"
    unzip -q "${archive}" xray -d "${temporary}"
    install -o root -g root -m 0755 "${temporary}/xray" /usr/local/bin/xray
    rm -rf "${temporary}"
}

install_baseline() {
    note "Applying a conservative TCP baseline"
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/configs/99-home-location-endpoint.conf" \
        /etc/sysctl.d/99-home-location-endpoint.conf
    modprobe tcp_bbr 2>/dev/null || true
    if sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw bbr; then
        printf '%s\n' 'net.ipv4.tcp_congestion_control = bbr' \
            >> /etc/sysctl.d/99-home-location-endpoint.conf
    else
        printf 'WARNING: this kernel does not expose BBR; the remaining settings were applied.\n' >&2
    fi
    sysctl --load=/etc/sysctl.d/99-home-location-endpoint.conf >/dev/null
}

create_accounts_and_directories() {
    getent group home-location >/dev/null || groupadd --system home-location
    id -u home-location >/dev/null 2>&1 || useradd \
        --system --gid home-location --home-dir /nonexistent \
        --shell /usr/sbin/nologin home-location
    getent group xray >/dev/null || groupadd --system xray
    id -u xray >/dev/null 2>&1 || useradd \
        --system --gid xray --home-dir /nonexistent \
        --shell /usr/sbin/nologin xray

    install -d -o root -g home-location -m 0750 "${ETC_DIR}"
    install -d -o root -g root -m 0755 "${APP_DIR}"
    install -d -o root -g home-location -m 0750 "${STATE_DIR}"
    install -d -o home-location -g home-location -m 0750 "${LOG_DIR}"
    install -d -o root -g xray -m 0750 "${XRAY_CONFIG_DIR}"

    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/interceptor.py" "${APP_DIR}/interceptor.py"
    install -o root -g root -m 0644 "${SOURCE_DIR}/src/home_location_endpoint/gsloc_rewrite.py" "${APP_DIR}/gsloc_rewrite.py"
    install -o root -g root -m 0644 "${SOURCE_DIR}/src/home_location_endpoint/wifitile_rewrite.py" "${APP_DIR}/wifitile_rewrite.py"
    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/location_picker.py" "${APP_DIR}/location_picker.py"
    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/cli.py" "${APP_DIR}/cli.py"
    ln -sfn "${APP_DIR}/cli.py" /usr/local/sbin/hle

    if [[ ! -f "${ETC_DIR}/jitter.seed" ]]; then
        umask 0077
        openssl rand 32 > "${ETC_DIR}/jitter.seed"
    fi
    chown root:home-location "${ETC_DIR}/jitter.seed"
    chmod 0640 "${ETC_DIR}/jitter.seed"
}

select_random_location() {
    note "Detecting the egress city and selecting a fresh random point"
    python3 "${APP_DIR}/location_picker.py" \
        --output "${ETC_DIR}/location.json" \
        --cache "${STATE_DIR}/city-boundary.json"
    chown root:home-location "${ETC_DIR}/location.json"
    chmod 0640 "${ETC_DIR}/location.json"
    if [[ -z "${SERVER}" ]]; then
        SERVER="$(python3 -c 'import json; print(json.load(open("/etc/home-location-endpoint/location.json"))["source"]["ip"])')"
    fi
}

generate_certificates() {
    local extension_file
    if [[ "${ROTATE_CA}" -eq 1 ]]; then
        rm -f "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der" \
            "${ETC_DIR}/leaf.crt" "${ETC_DIR}/leaf.key"
    fi
    if [[ -f "${ETC_DIR}/ca.crt" && -f "${ETC_DIR}/ca.der" && \
          -f "${ETC_DIR}/leaf.crt" && -f "${ETC_DIR}/leaf.key" ]]; then
        note "Reusing the existing scoped CA and leaf certificate"
        return
    fi

    note "Generating a scoped private CA and Apple-location leaf certificate"
    umask 0077
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
        -out "${ETC_DIR}/ca.key"
    openssl req -x509 -new -sha256 -days 3650 \
        -key "${ETC_DIR}/ca.key" -out "${ETC_DIR}/ca.crt" \
        -subj "/CN=Home Location Endpoint Root CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash"
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
        -out "${ETC_DIR}/leaf.key"
    openssl req -new -key "${ETC_DIR}/leaf.key" \
        -out "${ETC_DIR}/leaf.csr" -subj "/CN=gs-loc.apple.com"
    extension_file="$(mktemp)"
    cat > "${extension_file}" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:gs-loc.apple.com,DNS:gs-loc-cn.apple.com,DNS:*.ls.apple.com
EOF
    openssl x509 -req -sha256 -days 397 \
        -in "${ETC_DIR}/leaf.csr" \
        -CA "${ETC_DIR}/ca.crt" -CAkey "${ETC_DIR}/ca.key" -CAcreateserial \
        -extfile "${extension_file}" -out "${ETC_DIR}/leaf.crt"
    openssl x509 -in "${ETC_DIR}/ca.crt" -outform DER -out "${ETC_DIR}/ca.der"
    rm -f "${extension_file}" "${ETC_DIR}/leaf.csr" \
        "${ETC_DIR}/ca.srl" "${ETC_DIR}/ca.key"
    chown root:home-location "${ETC_DIR}/leaf.key" "${ETC_DIR}/leaf.crt"
    chmod 0640 "${ETC_DIR}/leaf.key" "${ETC_DIR}/leaf.crt"
    chmod 0644 "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der"
}

load_or_create_credentials() {
    local key_output
    if [[ -f "${ETC_DIR}/install.env" ]]; then
        # shellcheck disable=SC1091
        source "${ETC_DIR}/install.env"
        CLIENT_UUID="${HLE_UUID}"
        PRIVATE_KEY="${HLE_PRIVATE_KEY}"
        PUBLIC_KEY="${HLE_PUBLIC_KEY}"
        SHORT_ID="${HLE_SHORT_ID}"
        return
    fi
    CLIENT_UUID="$(/usr/local/bin/xray uuid)"
    key_output="$(/usr/local/bin/xray x25519)"
    PRIVATE_KEY="$(printf '%s\n' "${key_output}" | awk -F': *' '/^(PrivateKey|Private key):/{print $2; exit}')"
    PUBLIC_KEY="$(printf '%s\n' "${key_output}" | awk -F': *' '/^(Password \(PublicKey\)|Password|PublicKey|Public key):/{print $2; exit}')"
    SHORT_ID="$(openssl rand -hex 8)"
    [[ -n "${CLIENT_UUID}" && -n "${PRIVATE_KEY}" && -n "${PUBLIC_KEY}" && -n "${SHORT_ID}" ]] \
        || die "could not parse generated Xray credentials"
}

check_reality_target() {
    note "Validating the REALITY camouflage target"
    timeout 15 openssl s_client \
        -connect "${REALITY_TARGET}" -servername "${REALITY_SNI}" \
        -verify_hostname "${REALITY_SNI}" -verify_return_error -brief \
        </dev/null >/dev/null 2>&1 \
        || die "REALITY target ${REALITY_TARGET} did not validate for SNI ${REALITY_SNI}"
}

render_and_validate() {
    local stage
    note "Rendering and validating the endpoint configuration"
    stage="$(mktemp -d)"
    python3 "${SOURCE_DIR}/src/home_location_endpoint/render.py" \
        --config "${stage}/config.json" \
        --uri "${stage}/node-uri.txt" \
        --profile "${stage}/Home-Location-Endpoint-CA.mobileconfig" \
        --ca-der "${ETC_DIR}/ca.der" \
        --server "${SERVER}" --port "${PORT}" --uuid "${CLIENT_UUID}" \
        --reality-sni "${REALITY_SNI}" --reality-target "${REALITY_TARGET}" \
        --private-key "${PRIVATE_KEY}" --public-key "${PUBLIC_KEY}" \
        --short-id "${SHORT_ID}"
    /usr/local/bin/xray run -test -config "${stage}/config.json"
    if [[ -f "${XRAY_CONFIG_DIR}/config.json" ]]; then
        XRAY_BACKUP="$(mktemp /run/home-location-endpoint-xray.XXXXXX)"
        cp --preserve=mode,ownership "${XRAY_CONFIG_DIR}/config.json" "${XRAY_BACKUP}"
    fi
    install -o root -g xray -m 0640 "${stage}/config.json" "${XRAY_CONFIG_DIR}/config.json"
    install -o root -g root -m 0600 "${stage}/node-uri.txt" "${ETC_DIR}/node-uri.txt"
    install -o root -g root -m 0644 \
        "${stage}/Home-Location-Endpoint-CA.mobileconfig" \
        "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
    rm -rf "${stage}"

    umask 0077
    {
        printf 'HLE_PORT=%q\n' "${PORT}"
        printf 'HLE_SERVER=%q\n' "${SERVER}"
        printf 'HLE_REALITY_SNI=%q\n' "${REALITY_SNI}"
        printf 'HLE_REALITY_TARGET=%q\n' "${REALITY_TARGET}"
        printf 'HLE_UUID=%q\n' "${CLIENT_UUID}"
        printf 'HLE_PRIVATE_KEY=%q\n' "${PRIVATE_KEY}"
        printf 'HLE_PUBLIC_KEY=%q\n' "${PUBLIC_KEY}"
        printf 'HLE_SHORT_ID=%q\n' "${SHORT_ID}"
    } > "${ETC_DIR}/install.env"
    chmod 0600 "${ETC_DIR}/install.env"
    printf '%s\n' "${XRAY_VERSION}" > "${MARKER}"
    chmod 0600 "${MARKER}"
}

install_services() {
    note "Installing and starting the two scoped services"
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/systemd/home-location-endpoint.service" \
        /etc/systemd/system/home-location-endpoint.service
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/systemd/xray.service" \
        /etc/systemd/system/xray.service
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/configs/home-location-endpoint.logrotate" \
        /etc/logrotate.d/home-location-endpoint
    systemctl daemon-reload
    systemctl enable home-location-endpoint.service xray.service >/dev/null
    systemctl restart home-location-endpoint.service \
        || die "the location interceptor failed to start; inspect journalctl -u home-location-endpoint"
    if ! systemctl restart xray.service; then
        if [[ -n "${XRAY_BACKUP}" && -f "${XRAY_BACKUP}" ]]; then
            install -o root -g xray -m 0640 "${XRAY_BACKUP}" "${XRAY_CONFIG_DIR}/config.json"
            systemctl restart xray.service || true
        fi
        die "Xray failed to start; the previous Xray config was restored when available"
    fi
    if [[ -n "${XRAY_BACKUP}" && -f "${XRAY_BACKUP}" ]]; then
        rm -f "${XRAY_BACKUP}"
    fi
}

open_active_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
        note "Allowing TCP ${PORT} in the already-active UFW policy"
        ufw allow "${PORT}/tcp" comment "Home Location Endpoint" >/dev/null
    else
        printf 'Firewall note: UFW was not active, so the installer did not change firewall state.\n'
    fi
}

show_result() {
    local city fingerprint
    city="$(python3 -c 'import json; d=json.load(open("/etc/home-location-endpoint/location.json")); print(d["source"]["city"]+", "+d["source"]["country_code"])')"
    fingerprint="$(openssl x509 -in "${ETC_DIR}/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
    cat <<EOF

${PROJECT} is ready.

Random location city: ${city}
VLESS URI:
$(cat "${ETC_DIR}/node-uri.txt")

iOS CA profile: ${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig
CA SHA-256: ${fingerprint}

Next:
  1. Copy the profile to the iPhone, install it, then enable full trust for this CA.
  2. Import the VLESS URI into a full-tunnel client and connect through this endpoint.
  3. Run 'hle verify' and 'hle status' for local checks.
  4. Run 'sudo hle relocate' whenever you want another random point in the same IP city.

SSH was not changed. If a provider firewall exists, allow TCP ${PORT} there.
EOF
}

main() {
    require_root
    bootstrap_if_needed "$@"
    check_os
    check_existing_installation
    load_existing_settings
    parse_args "$@"
    install_packages
    check_port_available
    install_xray
    install_baseline
    create_accounts_and_directories
    select_random_location
    generate_certificates
    load_or_create_credentials
    check_reality_target
    render_and_validate
    install_services
    open_active_firewall
    /usr/local/sbin/hle verify
    show_result
}

main "$@"
