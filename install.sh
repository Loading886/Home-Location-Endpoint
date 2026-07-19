#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 022

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
PREVIOUS_PORT=""
SERVER=""
MODE=""
EXISTING_MODE=""
MODE_EXPLICIT=0
PROXY_OPTION_EXPLICIT=0
PROTOCOL_EXPLICIT=0
SERVER_EXPLICIT=0
PROXY_PROTOCOL="vless-reality"
EXISTING_PROXY_PROTOCOL=""
TELEGRAM_TOKEN=""
TELEGRAM_CHAT_ID=""
SS_PASSWORD=""
FALLBACK_UPLOAD_AFTER=""
FALLBACK_UPLOAD_RATE=""
FALLBACK_UPLOAD_BURST=""
FALLBACK_DOWNLOAD_AFTER=""
FALLBACK_DOWNLOAD_RATE=""
FALLBACK_DOWNLOAD_BURST=""
REALITY_SNI="www.usc.edu"
REALITY_TARGET="www.usc.edu:443"
LISTEN_ADDRESS="0.0.0.0"
ROTATE_CA=0
TRANSACTION_BACKUP=""
TRANSACTION_STARTED=0
TRANSACTION_COMMITTED=0
HOME_WAS_ACTIVE=0
HOME_WAS_ENABLED=0
XRAY_WAS_ACTIVE=0
XRAY_WAS_ENABLED=0
CREATED_HOME_USER=0
CREATED_HOME_GROUP=0
CREATED_XRAY_USER=0
CREATED_XRAY_GROUP=0
CREATED_BOT_USER=0
CREATED_BOT_GROUP=0
RUN_CREATED_HOME_USER=0
RUN_CREATED_HOME_GROUP=0
RUN_CREATED_XRAY_USER=0
RUN_CREATED_XRAY_GROUP=0
RUN_CREATED_BOT_USER=0
RUN_CREATED_BOT_GROUP=0
BOT_WAS_ACTIVE=0
BOT_WAS_ENABLED=0
TEMP_DIRS=()
ROLLBACK_PATHS=()

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\n==> %s\n' "$*"
}

proxy_mode() {
    [[ "${MODE:-}" == "full" || "${MODE:-}" == "advanced" ]]
}

advanced_mode() {
    [[ "${MODE:-}" == "advanced" ]]
}

print_help() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]

  --mode MODE              full, advanced, or modifier-only (interactive when omitted)
  --protocol PROTOCOL      vless-reality or ss2022 (advanced mode only)
  --port PORT              proxy listening port (default: 443, proxy modes)
  --server HOST_OR_IP      address written into the client URI (default: detected egress IP)
  --rotate-ca              replace the scoped CA and leaf certificate

Advanced mode installs a Telegram location bot. For unattended installation, pass
HLE_TELEGRAM_BOT_TOKEN and HLE_TELEGRAM_CHAT_ID in the environment.
--port and --server apply to proxy modes. REALITY SNI is fixed to www.usc.edu.
Remove a completed installation later with: sudo hle uninstall
The installer never changes SSH ports, keys, or passwords. Proxy modes may add their TCP port, plus UDP for SS2022, to an already-active UFW policy.
EOF
}

show_help_if_requested() {
    local argument
    for argument in "$@"; do
        if [[ "${argument}" == "-h" || "${argument}" == "--help" ]]; then
            print_help
            exit 0
        fi
    done
}

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    if [[ "${TRANSACTION_STARTED}" -eq 1 && "${TRANSACTION_COMMITTED}" -eq 0 ]]; then
        rollback_transaction
    fi
    local temporary
    for temporary in "${TEMP_DIRS[@]}"; do
        [[ -n "${temporary}" ]] && rm -rf -- "${temporary}"
    done
    exit "${status}"
}

trap cleanup EXIT

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run this installer as root"
}

register_temp_dir() {
    TEMP_DIRS+=("$1")
}

path_exists() {
    [[ -e "$1" || -L "$1" ]]
}

reject_symlink_if_present() {
    [[ ! -L "$1" ]] || die "managed path must not be a symlink: $1"
}

root_owned_not_group_world_writable() {
    [[ "$(stat -c %u "$1" 2>/dev/null || printf invalid)" == "0" \
      && -z "$(find "$1" -maxdepth 0 -perm /022 -print -quit 2>/dev/null)" ]]
}

restore_service_state() {
    local service="$1" was_active="$2" was_enabled="$3"
    if [[ "${was_enabled}" -eq 1 ]]; then
        systemctl enable "${service}" >/dev/null 2>&1 || true
    else
        systemctl disable "${service}" >/dev/null 2>&1 || true
    fi
    if [[ "${was_active}" -eq 1 ]]; then
        systemctl restart "${service}" >/dev/null 2>&1 || true
    else
        systemctl stop "${service}" >/dev/null 2>&1 || true
    fi
}

remove_account_created_this_run() {
    local user="$1" group="$2" user_created="$3" group_created="$4"
    if [[ "${user_created}" -eq 1 ]] && id -u "${user}" >/dev/null 2>&1; then
        userdel "${user}" >/dev/null 2>&1 || true
    fi
    if [[ "${group_created}" -eq 1 ]] && getent group "${group}" >/dev/null 2>&1; then
        groupdel "${group}" >/dev/null 2>&1 || true
    fi
}

rollback_transaction() {
    local index path backup_path state
    printf '\n==> Installation failed; restoring the previous managed state\n' >&2
    systemctl stop home-location-endpoint.service >/dev/null 2>&1 || true
    if advanced_mode; then
        systemctl stop home-location-telegram-bot.service >/dev/null 2>&1 || true
    fi
    if proxy_mode; then
        systemctl stop xray.service >/dev/null 2>&1 || true
    fi
    for index in "${!ROLLBACK_PATHS[@]}"; do
        path="${ROLLBACK_PATHS[${index}]}"
        backup_path="${TRANSACTION_BACKUP}/items/${index}"
        state="$(<"${TRANSACTION_BACKUP}/state/${index}")"
        rm -rf -- "${path}"
        if [[ "${state}" == "present" ]]; then
            mkdir -p -- "$(dirname -- "${path}")"
            cp -a -- "${backup_path}" "${path}"
        fi
    done
    systemctl daemon-reload >/dev/null 2>&1 || true
    restore_service_state home-location-endpoint.service \
        "${HOME_WAS_ACTIVE}" "${HOME_WAS_ENABLED}"
    if proxy_mode; then
        restore_service_state xray.service "${XRAY_WAS_ACTIVE}" "${XRAY_WAS_ENABLED}"
    fi
    if advanced_mode; then
        restore_service_state home-location-telegram-bot.service \
            "${BOT_WAS_ACTIVE}" "${BOT_WAS_ENABLED}"
    fi
    remove_account_created_this_run \
        home-location-bot home-location-bot \
        "${RUN_CREATED_BOT_USER}" "${RUN_CREATED_BOT_GROUP}"
    remove_account_created_this_run \
        xray xray "${RUN_CREATED_XRAY_USER}" "${RUN_CREATED_XRAY_GROUP}"
    remove_account_created_this_run \
        home-location home-location \
        "${RUN_CREATED_HOME_USER}" "${RUN_CREATED_HOME_GROUP}"
}

begin_transaction() {
    local index path
    TRANSACTION_BACKUP="$(mktemp -d)"
    register_temp_dir "${TRANSACTION_BACKUP}"
    mkdir -p "${TRANSACTION_BACKUP}/items" "${TRANSACTION_BACKUP}/state"
    ROLLBACK_PATHS=(
        "${ETC_DIR}"
        "${APP_DIR}"
        "${STATE_DIR}"
        "${LOG_DIR}"
        /usr/local/sbin/hle
        /etc/systemd/system/home-location-endpoint.service
        /etc/logrotate.d/home-location-endpoint
    )
    if proxy_mode; then
        ROLLBACK_PATHS+=(
            "${XRAY_CONFIG_DIR}"
            /usr/local/bin/xray
            /etc/systemd/system/xray.service
            /etc/sysctl.d/99-home-location-endpoint.conf
        )
    fi
    if advanced_mode; then
        ROLLBACK_PATHS+=(
            /etc/systemd/system/home-location-telegram-bot.service
            /var/backups/home-location-endpoint
        )
    fi
    systemctl is-active --quiet home-location-endpoint.service \
        >/dev/null 2>&1 && HOME_WAS_ACTIVE=1
    systemctl is-enabled --quiet home-location-endpoint.service \
        >/dev/null 2>&1 && HOME_WAS_ENABLED=1
    if proxy_mode; then
        systemctl is-active --quiet xray.service \
            >/dev/null 2>&1 && XRAY_WAS_ACTIVE=1
        systemctl is-enabled --quiet xray.service \
            >/dev/null 2>&1 && XRAY_WAS_ENABLED=1
    fi
    if advanced_mode; then
        systemctl is-active --quiet home-location-telegram-bot.service \
            >/dev/null 2>&1 && BOT_WAS_ACTIVE=1
        systemctl is-enabled --quiet home-location-telegram-bot.service \
            >/dev/null 2>&1 && BOT_WAS_ENABLED=1
    fi
    for index in "${!ROLLBACK_PATHS[@]}"; do
        path="${ROLLBACK_PATHS[${index}]}"
        if path_exists "${path}"; then
            cp -a -- "${path}" "${TRANSACTION_BACKUP}/items/${index}"
            printf '%s\n' present > "${TRANSACTION_BACKUP}/state/${index}"
        else
            printf '%s\n' missing > "${TRANSACTION_BACKUP}/state/${index}"
        fi
    done
    TRANSACTION_STARTED=1
}

preflight_common_state() {
    local path
    for path in "${ETC_DIR}" "${APP_DIR}" "${STATE_DIR}" "${LOG_DIR}"; do
        [[ ! -L "${path}" ]] || die "managed directory must not be a symlink: ${path}"
    done
    [[ ! -L "${MARKER}" ]] || die "installer marker must not be a symlink"
    for path in \
        "${ETC_DIR}/mode" "${ETC_DIR}/install.env" \
        "${ETC_DIR}/location.json" "${ETC_DIR}/jitter.seed" \
        "${STATE_DIR}/modifier.state" \
        "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der" \
        "${ETC_DIR}/leaf.crt" "${ETC_DIR}/leaf.key" \
        "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig" \
        "${ETC_DIR}/xray-location-routing.example.json" \
        "${ETC_DIR}/node-uri.txt" \
        "${ETC_DIR}/runtime.env" \
        "${ETC_DIR}/telegram/token" "${ETC_DIR}/telegram/chat_id" \
        "${APP_DIR}/interceptor.py" "${APP_DIR}/gsloc_rewrite.py" \
        "${APP_DIR}/wifitile_rewrite.py" "${APP_DIR}/location_picker.py" \
        "${APP_DIR}/preset_manager.py" "${APP_DIR}/telegram_bot.py" \
        "${APP_DIR}/cli.py" \
        /etc/systemd/system/home-location-endpoint.service \
        /etc/systemd/system/home-location-telegram-bot.service \
        /etc/logrotate.d/home-location-endpoint; do
        reject_symlink_if_present "${path}"
    done
    if [[ -f "${MARKER}" ]]; then
        [[ -L /usr/local/sbin/hle \
          && "$(readlink /usr/local/sbin/hle)" == "${APP_DIR}/cli.py" ]] \
            || die "managed hle command is missing or points to an unexpected target"
    fi
    if [[ ! -f "${MARKER}" ]] && {
        path_exists "${ETC_DIR}" ||
        path_exists "${APP_DIR}" ||
        path_exists /usr/local/sbin/hle ||
        path_exists /etc/systemd/system/home-location-endpoint.service;
    }; then
        die "partial or unmanaged Home-Location-Endpoint files already exist; inspect them before installing"
    fi
    if [[ -f "${MARKER}" ]]; then
        root_owned_not_group_world_writable "${ETC_DIR}" \
            || die "managed config directory ownership or permissions are unsafe"
        if [[ ! -f "${ETC_DIR}/install.env" ]] \
            || ! root_owned_not_group_world_writable "${ETC_DIR}/install.env"; then
            die "install.env must be a root-owned regular file without group/world write access"
        fi
        root_owned_not_group_world_writable "${MARKER}" \
            || die "installer marker ownership or permissions are unsafe"
    fi
    if [[ -f "${MARKER}" && ! -f "${ETC_DIR}/mode" && ! -f "${ETC_DIR}/install.env" ]]; then
        die "managed installation metadata is incomplete; refusing to guess its mode"
    fi
}

acquire_install_lock() {
    command -v flock >/dev/null 2>&1 || die "flock is required (install util-linux)"
    exec 9>/run/home-location-endpoint.lock
    flock -n 9 || die "another Home-Location-Endpoint operation is already running"
}

bootstrap_if_needed() {
    local script_dir archive_url temporary extracted
    [[ "${BOOTSTRAP_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] \
        || die "HLE_VERSION contains unsupported characters"
    if [[ -n "${HLE_BOOTSTRAP_TEMP:-}" ]]; then
        [[ -d "${HLE_BOOTSTRAP_TEMP}" \
          && ! -L "${HLE_BOOTSTRAP_TEMP}" \
          && -f "${HLE_BOOTSTRAP_TEMP}/.home-location-endpoint-bootstrap" \
          && "$(stat -c %u "${HLE_BOOTSTRAP_TEMP}")" == "0" ]] \
            || die "refusing an untrusted HLE_BOOTSTRAP_TEMP directory"
        register_temp_dir "${HLE_BOOTSTRAP_TEMP}"
        unset HLE_BOOTSTRAP_TEMP
    fi
    if ! script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"; then
        script_dir=""
    fi
    if [[ -f "${script_dir}/src/home_location_endpoint/interceptor.py" ]]; then
        SOURCE_DIR="${script_dir}"
        return
    fi

    note "Downloading ${PROJECT} ${BOOTSTRAP_VERSION}"
    printf '下载中……请稍等\n'
    wait_for_apt_lock
    apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 update -qq
    # Ubuntu 24.04 defaults needrestart to automatically restarting every
    # affected daemon. A remote endpoint installer must not bounce unrelated
    # network/login services; report pending restarts and leave that decision
    # to the operator.
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
        apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 install -y -qq ca-certificates curl tar util-linux
    temporary="$(mktemp -d)"
    register_temp_dir "${temporary}"
    : > "${temporary}/.home-location-endpoint-bootstrap"
    if [[ "${BOOTSTRAP_VERSION}" == "main" ]]; then
        archive_url="${REPOSITORY}/archive/refs/heads/main.tar.gz"
    else
        archive_url="${REPOSITORY}/archive/refs/tags/${BOOTSTRAP_VERSION}.tar.gz"
    fi
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 180 --retry 3 --retry-all-errors \
        "${archive_url}" -o "${temporary}/source.tar.gz"
    tar -xzf "${temporary}/source.tar.gz" -C "${temporary}"
    extracted="$(find "${temporary}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    [[ -n "${extracted}" ]] || die "downloaded archive did not contain a source directory"
    [[ -f "${extracted}/install.sh" \
      && -f "${extracted}/src/home_location_endpoint/interceptor.py" \
      && -f "${extracted}/src/home_location_endpoint/reality_probe.py" ]] \
        || die "downloaded archive is missing required project files"
    HLE_BOOTSTRAP_TEMP="${temporary}" exec bash "${extracted}/install.sh" "$@"
}

load_existing_settings() {
    local mode_from_file="" mode_from_env="" inventory_flag
    if [[ -f "${ETC_DIR}/mode" ]]; then
        mode_from_file="$(<"${ETC_DIR}/mode")"
        case "${mode_from_file}" in
            full|advanced|modifier-only) ;;
            *) die "invalid recorded installation mode: ${mode_from_file}" ;;
        esac
    fi
    if [[ -f "${ETC_DIR}/install.env" ]]; then
        # This file is created root-owned and mode 0600 by this installer.
        # shellcheck disable=SC1091
        source "${ETC_DIR}/install.env"
        mode_from_env="${HLE_MODE:-full}"
        case "${mode_from_env}" in
            full|advanced|modifier-only) ;;
            *) die "invalid installation mode in install.env: ${mode_from_env}" ;;
        esac
        PORT="${HLE_PORT:-${PORT}}"
        PREVIOUS_PORT="${HLE_PORT:-}"
        SERVER="${HLE_SERVER:-${SERVER}}"
        SERVER_EXPLICIT="${HLE_SERVER_EXPLICIT:-${SERVER_EXPLICIT}}"
        CREATED_HOME_USER="${HLE_CREATED_HOME_USER:-${CREATED_HOME_USER}}"
        CREATED_HOME_GROUP="${HLE_CREATED_HOME_GROUP:-${CREATED_HOME_GROUP}}"
        CREATED_XRAY_USER="${HLE_CREATED_XRAY_USER:-${CREATED_XRAY_USER}}"
        CREATED_XRAY_GROUP="${HLE_CREATED_XRAY_GROUP:-${CREATED_XRAY_GROUP}}"
        CREATED_BOT_USER="${HLE_CREATED_BOT_USER:-${CREATED_BOT_USER}}"
        CREATED_BOT_GROUP="${HLE_CREATED_BOT_GROUP:-${CREATED_BOT_GROUP}}"
        PROXY_PROTOCOL="${HLE_PROXY_PROTOCOL:-vless-reality}"
        EXISTING_PROXY_PROTOCOL="${PROXY_PROTOCOL}"
        for inventory_flag in \
            SERVER_EXPLICIT CREATED_HOME_USER CREATED_HOME_GROUP \
            CREATED_XRAY_USER CREATED_XRAY_GROUP \
            CREATED_BOT_USER CREATED_BOT_GROUP; do
            case "${!inventory_flag}" in
                0|1) ;;
                *) die "invalid installation inventory flag: ${inventory_flag}" ;;
            esac
        done
    fi
    if [[ -n "${mode_from_file}" && -n "${mode_from_env}" \
          && "${mode_from_file}" != "${mode_from_env}" ]]; then
        die "installation mode records disagree; refusing to guess"
    fi
    EXISTING_MODE="${mode_from_file:-${mode_from_env}}"
    MODE="${EXISTING_MODE:-${MODE}}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --mode)
                [[ $# -ge 2 ]] || die "--mode needs a value"
                MODE="$2"
                MODE_EXPLICIT=1
                shift 2
                ;;
            --port)
                [[ $# -ge 2 ]] || die "--port needs a value"
                PORT="$2"
                PROXY_OPTION_EXPLICIT=1
                shift 2
                ;;
            --protocol)
                [[ $# -ge 2 ]] || die "--protocol needs a value"
                PROXY_PROTOCOL="$2"
                PROTOCOL_EXPLICIT=1
                PROXY_OPTION_EXPLICIT=1
                shift 2
                ;;
            --server)
                [[ $# -ge 2 ]] || die "--server needs a value"
                SERVER="$2"
                SERVER_EXPLICIT=1
                PROXY_OPTION_EXPLICIT=1
                shift 2
                ;;
            --rotate-ca)
                ROTATE_CA=1
                shift
                ;;
            -h|--help)
                print_help
                exit 0
                ;;
            *)
                die "unknown option: $1"
                ;;
        esac
    done
    [[ "${PORT}" =~ ^[0-9]+$ ]] || die "port must be numeric"
    (( PORT >= 1 && PORT <= 65535 )) || die "port must be between 1 and 65535"
    if [[ "${MODE_EXPLICIT}" -eq 1 \
          && "${MODE}" != "full" && "${MODE}" != "advanced" \
          && "${MODE}" != "modifier-only" ]]; then
        die "--mode must be full, advanced, or modifier-only"
    fi
    case "${PROXY_PROTOCOL}" in
        vless-reality|ss2022) ;;
        *) die "--protocol must be vless-reality or ss2022" ;;
    esac
}

select_install_mode() {
    local choice=""
    if [[ -n "${EXISTING_MODE}" ]]; then
        if [[ "${MODE_EXPLICIT}" -eq 1 && "${MODE}" != "${EXISTING_MODE}" ]]; then
            die "changing an existing install from ${EXISTING_MODE} to ${MODE} is not supported"
        fi
        MODE="${EXISTING_MODE}"
    elif [[ -z "${MODE}" ]]; then
        # [[ -r /dev/tty ]] is not enough: the device node can pass the readable
        # test while opening it fails with ENXIO when there is no controlling
        # terminal (cloud-init, Ansible, cron, systemd, nohup). Probe by actually
        # opening it so those environments fall back to full mode instead of
        # aborting under set -e on the first /dev/tty write.
        if { exec 3<>/dev/tty; } 2>/dev/null; then
            cat >&3 <<'EOF'

Choose an installation mode:
  1) Full proxy endpoint + location modifier (recommended)
  2) Proxy endpoint + Telegram location controller (advanced)
  3) Location modifier only (expert; integrate your own proxy)

选择安装模式：
  1) 新手模式：安装完整代理节点和定位修改器（推荐）
  2) 进阶模式：增加 Telegram 定位菜单，并可选择接入协议
  3) 高手模式：仅安装定位修改器，需要自行配置代理接入
EOF
            printf 'Selection / 请选择 [1]: ' >&3
            read -r choice <&3 || true
            exec 3>&-
            case "${choice:-1}" in
                1) MODE="full" ;;
                2) MODE="advanced" ;;
                3) MODE="modifier-only" ;;
                *) die "invalid installation mode selection" ;;
            esac
        else
            MODE="full"
            note "No interactive terminal detected; selecting full mode"
        fi
    fi
    case "${MODE}" in
        full|advanced|modifier-only) ;;
        *) die "mode must be full, advanced, or modifier-only" ;;
    esac
    if [[ "${MODE}" == "modifier-only" && "${PROXY_OPTION_EXPLICIT}" -eq 1 ]]; then
        die "--port, --server, and --protocol apply only to proxy modes"
    fi
    if proxy_mode && [[ -n "${EXISTING_MODE}" \
          && "${PROTOCOL_EXPLICIT}" -eq 1 \
          && "${PROXY_PROTOCOL}" != "${EXISTING_PROXY_PROTOCOL}" ]]; then
        die "changing an existing proxy protocol in place is not supported"
    fi
    if [[ "${MODE}" == "full" && "${PROXY_PROTOCOL}" != "vless-reality" ]]; then
        die "SS2022 is available in advanced mode; beginner mode stays VLESS + REALITY"
    fi
}

select_advanced_options() {
    local choice="" tty_open=0
    advanced_mode || return 0

    if [[ -n "${EXISTING_MODE}" ]]; then
        [[ -f "${ETC_DIR}/telegram/token" && -f "${ETC_DIR}/telegram/chat_id" ]] \
            || die "advanced installation is missing Telegram credentials"
        TELEGRAM_TOKEN="$(<"${ETC_DIR}/telegram/token")"
        TELEGRAM_CHAT_ID="$(<"${ETC_DIR}/telegram/chat_id")"
    else
        if [[ "${PROTOCOL_EXPLICIT}" -eq 0 ]]; then
            if { exec 3<>/dev/tty; } 2>/dev/null; then
                tty_open=1
                cat >&3 <<'EOF'

Choose the proxy protocol for advanced mode:
  1) VLESS + REALITY + Vision (recommended for public-network use)
  2) Shadowsocks 2022 (2022-blake3-aes-256-gcm)

选择进阶模式的代理协议：
  1) VLESS + REALITY + Vision（公网接入推荐）
  2) Shadowsocks 2022（2022-blake3-aes-256-gcm）
EOF
                printf 'Protocol / 协议 [1]: ' >&3
                read -r choice <&3 || true
                case "${choice:-1}" in
                    1) PROXY_PROTOCOL="vless-reality" ;;
                    2) PROXY_PROTOCOL="ss2022" ;;
                    *) exec 3>&-; die "invalid protocol selection" ;;
                esac
            else
                die "advanced unattended install requires --protocol"
            fi
        fi

        TELEGRAM_TOKEN="${HLE_TELEGRAM_BOT_TOKEN:-}"
        TELEGRAM_CHAT_ID="${HLE_TELEGRAM_CHAT_ID:-}"
        if [[ -z "${TELEGRAM_TOKEN}" || -z "${TELEGRAM_CHAT_ID}" ]]; then
            if [[ "${tty_open}" -eq 0 ]] && ! { exec 3<>/dev/tty; } 2>/dev/null; then
                die "advanced unattended install requires HLE_TELEGRAM_BOT_TOKEN and HLE_TELEGRAM_CHAT_ID"
            fi
            tty_open=1
            cat >&3 <<'EOF'

Create a dedicated bot with @BotFather, send /start to that bot, then paste the
token and your numeric chat ID below. The token is hidden while typing.

请先通过 @BotFather 创建一个专用 Bot，并向它发送 /start；然后在下方粘贴
Bot Token 和你自己的数字 Chat ID。输入 Token 时终端不会显示字符。
EOF
            printf 'Bot token / Bot Token: ' >&3
            read -rs TELEGRAM_TOKEN <&3 || true
            printf '\nChat ID / Chat ID: ' >&3
            read -r TELEGRAM_CHAT_ID <&3 || true
        fi
        [[ "${tty_open}" -eq 0 ]] || exec 3>&-
    fi

    [[ "${TELEGRAM_TOKEN}" =~ ^[0-9]{6,20}:[A-Za-z0-9_-]{20,100}$ ]] \
        || die "Telegram bot token format is invalid"
    [[ "${TELEGRAM_CHAT_ID}" =~ ^-?[0-9]{5,20}$ ]] \
        || die "Telegram chat ID format is invalid"

    note "Validating the Telegram bot and authorized chat"
    HLE_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}" \
    HLE_TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID}" \
    PYTHONPATH="${SOURCE_DIR}/src" \
        python3 -m home_location_endpoint.telegram_bot --validate-credentials \
        || die "Telegram validation failed; send /start to the bot and check both values"
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
    if proxy_mode; then
        case "$(dpkg --print-architecture)" in
            amd64)
                XRAY_ASSET="Xray-linux-64.zip"
                XRAY_SHA256="${XRAY_AMD64_SHA256}"
                ;;
            arm64)
                XRAY_ASSET="Xray-linux-arm64-v8a.zip"
                XRAY_SHA256="${XRAY_ARM64_SHA256}"
                ;;
            *) die "proxy modes support amd64 and arm64; modifier-only is architecture independent" ;;
        esac
    fi
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
    python3 - "${PORT}" "${LISTEN_ADDRESS}" <<'PY'
import socket
import sys

port = int(sys.argv[1])
host = sys.argv[2]
family = socket.AF_INET6 if ":" in host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((host, port, 0, 0))
    else:
        sock.bind((host, port))
except OSError as exc:
    raise SystemExit("TCP port %d is unavailable: %s" % (port, exc))
finally:
    sock.close()
PY
    if [[ "${PROXY_PROTOCOL}" == "ss2022" ]]; then
        python3 - "${PORT}" "${LISTEN_ADDRESS}" <<'PY'
import socket
import sys

port = int(sys.argv[1])
host = sys.argv[2]
family = socket.AF_INET6 if ":" in host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_DGRAM)
try:
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((host, port, 0, 0))
    else:
        sock.bind((host, port))
except OSError as exc:
    raise SystemExit("UDP port %d is unavailable: %s" % (port, exc))
finally:
    sock.close()
PY
    fi
}

detect_listen_address() {
    LISTEN_ADDRESS="0.0.0.0"
    if [[ -s /proc/net/if_inet6 \
          && "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || printf 0)" != "1" ]] \
        && python3 - <<'PY'
import socket

sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
try:
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", 0, 0, 0))
finally:
    sock.close()
PY
    then
        LISTEN_ADDRESS="::"
        printf 'IPv6 is available; Xray will use an explicit IPv4/IPv6 dual-stack listener.\n'
    fi
}

check_existing_installation() {
    if proxy_mode && [[ -L "${XRAY_CONFIG_DIR}" ]]; then
        die "Xray config directory must not be a symlink: ${XRAY_CONFIG_DIR}"
    fi
    if proxy_mode; then
        reject_symlink_if_present "${XRAY_CONFIG_DIR}/config.json"
        reject_symlink_if_present /usr/local/bin/xray
        reject_symlink_if_present /etc/systemd/system/xray.service
        reject_symlink_if_present /etc/sysctl.d/99-home-location-endpoint.conf
    fi
    if proxy_mode && [[ ! -f "${MARKER}" ]] && {
        [[ -f "${XRAY_CONFIG_DIR}/config.json" ]] ||
        [[ -f /etc/systemd/system/xray.service ]] ||
        [[ -f /lib/systemd/system/xray.service ]] ||
        path_exists /usr/local/bin/xray;
    }; then
        die "an unmanaged Xray installation already exists; use a clean landing server"
    fi
    if advanced_mode && [[ ! -f "${MARKER}" ]] && {
        [[ -f /etc/systemd/system/home-location-telegram-bot.service ]] ||
        path_exists "${ETC_DIR}/telegram" ||
        path_exists "${STATE_DIR}/control";
    }; then
        die "unmanaged Telegram location-control files already exist; inspect them first"
    fi
}

check_resources() {
    local available_kb minimum_kb memory_kb
    available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
    minimum_kb=51200
    proxy_mode && minimum_kb=204800
    if [[ ! "${available_kb}" =~ ^[0-9]+$ || "${available_kb}" -lt "${minimum_kb}" ]]; then
        die "insufficient free disk space; ${MODE} mode needs at least $((minimum_kb / 1024)) MiB"
    fi
    memory_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
    if [[ "${memory_kb}" =~ ^[0-9]+$ && "${memory_kb}" -lt 393216 ]]; then
        printf 'WARNING: less than 384 MiB RAM detected; concurrent proxy/location load may be unstable.\n' >&2
    fi
}

wait_for_apt_lock() {
    # Fresh Ubuntu images run unattended-upgrades on first boot, which can hold
    # the dpkg/apt lock for many minutes; apt-get would otherwise fail at once
    # with a lock error and abort the install. Wait for the lock to clear with a
    # clear message and a bounded, overridable timeout. Needs fuser (psmisc,
    # present on the Ubuntu images where this happens); when it is unavailable
    # this is a no-op and the apt DPkg::Lock::Timeout option is the only backstop.
    local lock waited=0 announced=0
    local timeout_s="${HLE_APT_LOCK_WAIT:-600}"
    if [[ ! "${timeout_s}" =~ ^[0-9]+$ ]] || (( timeout_s > 3600 )); then
        die "HLE_APT_LOCK_WAIT must be an integer between 0 and 3600 seconds"
    fi
    command -v fuser >/dev/null 2>&1 || return 0
    local locks=(
        /var/lib/dpkg/lock-frontend
        /var/lib/dpkg/lock
        /var/lib/apt/lists/lock
    )
    while :; do
        local held=0
        for lock in "${locks[@]}"; do
            if [[ -e "${lock}" ]] && fuser "${lock}" >/dev/null 2>&1; then
                held=1
                break
            fi
        done
        [[ "${held}" -eq 0 ]] && break
        if [[ "${announced}" -eq 0 ]]; then
            note "Waiting for another package operation to release the apt lock (Ubuntu often runs unattended-upgrades on first boot)"
            announced=1
        fi
        if (( waited >= timeout_s )); then
            die "the apt/dpkg lock is still held after ${timeout_s}s. A background upgrade is likely running; check 'systemctl status unattended-upgrades apt-daily.service apt-daily-upgrade.service', let it finish, then re-run the installer (set HLE_APT_LOCK_WAIT to wait longer)."
        fi
        sleep 5
        waited=$((waited + 5))
        if (( waited % 30 == 0 )); then
            printf '  still waiting for the apt lock (%ds elapsed)...\n' "${waited}" >&2
        fi
    done
    # Note: the trailing statement must not be a false test, or the function
    # returns non-zero and aborts the install under set -e when the lock was free.
    if [[ "${announced}" -eq 1 ]]; then
        note "apt lock released; continuing"
    fi
    return 0
}

install_packages() {
    note "Installing required packages"
    wait_for_apt_lock
    apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 update -qq
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
        apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 install -y -qq \
        ca-certificates curl logrotate openssl python3 util-linux
    if apt-cache show qrencode >/dev/null 2>&1; then
        if ! DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
            apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 install -y -qq \
            qrencode; then
            printf 'WARNING: optional qrencode installation failed; profile download URLs will still work.\n' >&2
        fi
    else
        note "Optional qrencode package is unavailable; profile download URLs will still work"
    fi
    if proxy_mode; then
        DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
            apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 install -y -qq \
            iproute2 kmod procps unzip uuid-runtime
    fi
}

validate_explicit_overrides() {
    # Reject a malformed --server before the transaction, the Xray download,
    # and certificate generation. Runs after install_packages so python3 exists.
    if [[ "${SERVER_EXPLICIT}" -eq 1 && -n "${SERVER}" ]]; then
        PYTHONPATH="${SOURCE_DIR}/src" python3 -c \
            'import sys; from home_location_endpoint.render import validate_host; validate_host(sys.argv[1], allow_ip=True)' \
            "${SERVER}" 2>/dev/null || die "invalid --server address: ${SERVER}"
    fi
}

tls_target_works() {
    local sni="$1" target="$2"
    PYTHONPATH="${SOURCE_DIR}/src" python3 -m home_location_endpoint.reality_probe \
        "${sni}" "${target}" >/dev/null 2>&1
}

validate_fixed_reality_target() {
    note "Validating fixed REALITY SNI ${REALITY_SNI} with TLS 1.3 and HTTP/2"
    tls_target_works "${REALITY_SNI}" "${REALITY_TARGET}" \
        || die "fixed REALITY target ${REALITY_TARGET} did not validate for SNI ${REALITY_SNI}"
}

generate_fallback_limits() {
    read -r FALLBACK_UPLOAD_AFTER FALLBACK_UPLOAD_RATE FALLBACK_UPLOAD_BURST \
        FALLBACK_DOWNLOAD_AFTER FALLBACK_DOWNLOAD_RATE FALLBACK_DOWNLOAD_BURST < <(
        python3 -c 'import secrets
def pick(low, high): return low + secrets.randbelow(high - low + 1)
values = []
for _ in range(2):
    after = pick(4 * 1024 * 1024, 12 * 1024 * 1024)
    rate = pick(512 * 1024, 1024 * 1024)
    burst = pick(2 * 1024 * 1024, 6 * 1024 * 1024)
    values.extend((after, rate, max(rate, burst)))
print(*values)'
    )
}

install_xray() {
    local temporary archive
    note "Installing verified Xray ${XRAY_VERSION}"
    temporary="$(mktemp -d)"
    register_temp_dir "${temporary}"
    archive="${temporary}/${XRAY_ASSET}"
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 180 --retry 3 --retry-all-errors \
        "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${XRAY_ASSET}" \
        -o "${archive}"
    printf '%s  %s\n' "${XRAY_SHA256}" "${archive}" | sha256sum --check --status \
        || die "Xray archive checksum mismatch"
    unzip -q "${archive}" xray -d "${temporary}"
    install -o root -g root -m 0755 "${temporary}/xray" /usr/local/bin/xray
    rm -rf "${temporary}"
}

install_baseline() {
    note "Staging a conservative TCP baseline"
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
}

apply_baseline() {
    note "Applying the committed TCP baseline"
    if ! sysctl --load=/etc/sysctl.d/99-home-location-endpoint.conf >/dev/null; then
        printf 'WARNING: this host rejected part of the optional TCP baseline; installation will continue.\n' >&2
    fi
}

ensure_system_account() {
    local user="$1" group="$2" user_flag="$3" group_flag="$4"
    local run_user_flag="$5" run_group_flag="$6"
    local gid uid primary_group shell
    if ! getent group "${group}" >/dev/null; then
        groupadd --system "${group}"
        printf -v "${group_flag}" '%s' 1
        printf -v "${run_group_flag}" '%s' 1
    fi
    gid="$(getent group "${group}" | cut -d: -f3)"
    [[ "${gid}" =~ ^[0-9]+$ && "${gid}" -lt 1000 ]] \
        || die "existing group ${group} is not a compatible system group"
    if id -u "${user}" >/dev/null 2>&1; then
        uid="$(id -u "${user}")"
        primary_group="$(id -gn "${user}")"
        shell="$(getent passwd "${user}" | cut -d: -f7)"
        [[ "${uid}" -lt 1000 && "${primary_group}" == "${group}" ]] \
            || die "existing account ${user} is not a compatible system account"
        case "${shell}" in
            /usr/sbin/nologin|/sbin/nologin|/bin/false) ;;
            *) die "existing account ${user} has an interactive shell" ;;
        esac
        return
    fi
    useradd --system --gid "${group}" --home-dir /nonexistent \
        --shell /usr/sbin/nologin "${user}"
    printf -v "${user_flag}" '%s' 1
    printf -v "${run_user_flag}" '%s' 1
}

create_accounts_and_directories() {
    ensure_system_account \
        home-location home-location \
        CREATED_HOME_USER CREATED_HOME_GROUP \
        RUN_CREATED_HOME_USER RUN_CREATED_HOME_GROUP

    install -d -o root -g home-location -m 0750 "${ETC_DIR}"
    install -d -o root -g root -m 0755 "${APP_DIR}"
    install -d -o root -g home-location -m 0750 "${STATE_DIR}"
    install -d -o home-location -g home-location -m 0750 "${LOG_DIR}"
    if proxy_mode; then
        ensure_system_account xray xray \
            CREATED_XRAY_USER CREATED_XRAY_GROUP \
            RUN_CREATED_XRAY_USER RUN_CREATED_XRAY_GROUP
        install -d -o root -g xray -m 0750 "${XRAY_CONFIG_DIR}"
    fi
    if advanced_mode; then
        ensure_system_account \
            home-location-bot home-location-bot \
            CREATED_BOT_USER CREATED_BOT_GROUP \
            RUN_CREATED_BOT_USER RUN_CREATED_BOT_GROUP
        usermod -a -G home-location home-location-bot
        install -d -o root -g home-location-bot -m 0750 "${ETC_DIR}/telegram"
        install -d -o home-location-bot -g home-location -m 0750 \
            "${STATE_DIR}/control"
        install -d -o home-location-bot -g home-location-bot -m 0700 \
            /var/backups/home-location-endpoint
    fi

    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/interceptor.py" "${APP_DIR}/interceptor.py"
    install -o root -g root -m 0644 "${SOURCE_DIR}/src/home_location_endpoint/gsloc_rewrite.py" "${APP_DIR}/gsloc_rewrite.py"
    install -o root -g root -m 0644 "${SOURCE_DIR}/src/home_location_endpoint/wifitile_rewrite.py" "${APP_DIR}/wifitile_rewrite.py"
    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/location_picker.py" "${APP_DIR}/location_picker.py"
    install -o root -g root -m 0644 "${SOURCE_DIR}/src/home_location_endpoint/preset_manager.py" "${APP_DIR}/preset_manager.py"
    if advanced_mode; then
        install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/telegram_bot.py" "${APP_DIR}/telegram_bot.py"
    fi
    install -o root -g root -m 0755 "${SOURCE_DIR}/src/home_location_endpoint/cli.py" "${APP_DIR}/cli.py"
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/configs/xray-location-routing.example.json" \
        "${ETC_DIR}/xray-location-routing.example.json"
    ln -sfn "${APP_DIR}/cli.py" /usr/local/sbin/hle

    if [[ ! -f "${ETC_DIR}/jitter.seed" ]]; then
        umask 0077
        openssl rand 32 > "${ETC_DIR}/jitter.seed"
    fi
    chown root:home-location "${ETC_DIR}/jitter.seed"
    chmod 0640 "${ETC_DIR}/jitter.seed"
    if [[ ! -f "${STATE_DIR}/modifier.state" ]]; then
        printf 'active\n' > "${STATE_DIR}/modifier.state"
    fi
    case "$(<"${STATE_DIR}/modifier.state")" in
        active|paused) ;;
        *) die "invalid persistent modifier state" ;;
    esac
    chown root:root "${STATE_DIR}/modifier.state"
    chmod 0644 "${STATE_DIR}/modifier.state"
}

prepare_advanced_control() {
    local control_dir="${STATE_DIR}/control"
    local location_file="${control_dir}/location.json"
    local modifier_file="${control_dir}/modifier.state"
    local bot_uid bot_gid home_gid
    advanced_mode || return 0
    bot_uid="$(id -u home-location-bot)"
    bot_gid="$(id -g home-location-bot)"
    home_gid="$(id -g home-location)"

    if [[ -z "${EXISTING_MODE}" ]]; then
        PYTHONPATH="${SOURCE_DIR}/src" python3 - \
            "${ETC_DIR}/location.json" \
            "${SOURCE_DIR}/configs/advanced-location-catalog.json" \
            "${location_file}" <<'PY'
import json
import sys
from pathlib import Path

from home_location_endpoint import preset_manager

source = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
catalog = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
output = preset_manager.build_advanced_config(source, catalog)
preset_manager.atomic_write(Path(sys.argv[3]), output)
PY
        install -o home-location-bot -g home-location -m 0640 \
            "${STATE_DIR}/modifier.state" "${modifier_file}"
    else
        [[ -f "${location_file}" && -f "${modifier_file}" ]] \
            || die "advanced control state is incomplete"
        PYTHONPATH="${SOURCE_DIR}/src" python3 -c \
            'import sys; from home_location_endpoint.preset_manager import load; load(sys.argv[1])' \
            "${location_file}" || die "advanced location presets are invalid"
        case "$(<"${modifier_file}")" in
            active|paused) ;;
            *) die "advanced modifier state is invalid" ;;
        esac
    fi

    chown home-location-bot:home-location "${location_file}" "${modifier_file}"
    chmod 0640 "${location_file}" "${modifier_file}"
    cat > "${ETC_DIR}/runtime.env" <<EOF
GSLOC_PRESETS=${location_file}
GSLOC_MODIFIER_STATE=${modifier_file}
EOF
    chown root:root "${ETC_DIR}/runtime.env"
    chmod 0644 "${ETC_DIR}/runtime.env"
    printf '%s\n' "${TELEGRAM_TOKEN}" > "${ETC_DIR}/telegram/token"
    printf '%s\n' "${TELEGRAM_CHAT_ID}" > "${ETC_DIR}/telegram/chat_id"
    chown root:home-location-bot \
        "${ETC_DIR}/telegram/token" "${ETC_DIR}/telegram/chat_id"
    chmod 0640 "${ETC_DIR}/telegram/token" "${ETC_DIR}/telegram/chat_id"

    [[ "$(stat -c %u "${location_file}")" == "${bot_uid}" \
      && "$(stat -c %g "${location_file}")" == "${home_gid}" \
      && "$(stat -c %g "${ETC_DIR}/telegram/token")" == "${bot_gid}" ]] \
        || die "advanced control ownership validation failed"
}

select_random_location() {
    local output_gid
    note "Detecting the egress city and selecting a fresh random point"
    output_gid="$(id -g home-location)"
    if ! python3 "${APP_DIR}/location_picker.py" \
        --output "${ETC_DIR}/location.json" \
        --cache "${STATE_DIR}/city-boundary.json" \
        --output-mode 0640 --output-uid 0 --output-gid "${output_gid}"; then
        if [[ -f "${MARKER}" ]] && PYTHONPATH="${SOURCE_DIR}/src" \
            HLE_ETC="${ETC_DIR}" python3 -c \
            'from home_location_endpoint.cli import location_is_valid; raise SystemExit(0 if location_is_valid() else 1)'; then
            printf 'WARNING: location providers are unavailable; preserving the validated existing point.\n' >&2
        else
            die "could not select a location and no valid previous location is available"
        fi
    fi
    if [[ -z "${SERVER}" ]]; then
        SERVER="$(python3 -c 'import json; print(json.load(open("/etc/home-location-endpoint/location.json"))["source"]["ip"])')"
    fi
}

generate_certificates() {
    local extension_file stage present_count=0 path
    local -a certificate_paths=(
        "${ETC_DIR}/ca.crt"
        "${ETC_DIR}/ca.der"
        "${ETC_DIR}/leaf.crt"
        "${ETC_DIR}/leaf.key"
    )
    for path in "${certificate_paths[@]}"; do
        [[ -f "${path}" ]] && present_count=$((present_count + 1))
    done
    if [[ "${ROTATE_CA}" -eq 0 && "${present_count}" -eq 4 ]]; then
        validate_certificate_set \
            "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der" \
            "${ETC_DIR}/leaf.crt" "${ETC_DIR}/leaf.key" \
            || die "existing certificates are invalid or expire within 30 days; rerun with --rotate-ca and reinstall the iOS profile"
        note "Reusing the existing scoped CA and leaf certificate"
        return
    fi
    if [[ "${ROTATE_CA}" -eq 0 && "${present_count}" -ne 0 ]]; then
        die "certificate set is incomplete; rerun with --rotate-ca after reviewing the existing files"
    fi

    note "Generating a scoped private CA and Apple-location leaf certificate"
    stage="$(mktemp -d)"
    register_temp_dir "${stage}"
    umask 0077
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
        -out "${stage}/ca.key"
    openssl req -x509 -new -sha256 -days 3650 \
        -key "${stage}/ca.key" -out "${stage}/ca.crt" \
        -subj "/CN=Home Location Endpoint Root CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash"
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
        -out "${stage}/leaf.key"
    openssl req -new -key "${stage}/leaf.key" \
        -out "${stage}/leaf.csr" -subj "/CN=gs-loc.apple.com"
    extension_file="$(mktemp)"
    register_temp_dir "${extension_file}"
    cat > "${extension_file}" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:gs-loc.apple.com,DNS:gs-loc-cn.apple.com,DNS:*.ls.apple.com
EOF
    openssl x509 -req -sha256 -days 397 \
        -in "${stage}/leaf.csr" \
        -CA "${stage}/ca.crt" -CAkey "${stage}/ca.key" -CAcreateserial \
        -extfile "${extension_file}" -out "${stage}/leaf.crt"
    openssl x509 -in "${stage}/ca.crt" -outform DER -out "${stage}/ca.der"
    rm -f "${extension_file}" "${stage}/leaf.csr" "${stage}/ca.srl"
    validate_certificate_set \
        "${stage}/ca.crt" "${stage}/ca.der" \
        "${stage}/leaf.crt" "${stage}/leaf.key" \
        || die "newly generated certificate set failed validation"
    install -o root -g root -m 0644 "${stage}/ca.crt" "${ETC_DIR}/ca.crt"
    install -o root -g root -m 0644 "${stage}/ca.der" "${ETC_DIR}/ca.der"
    install -o root -g home-location -m 0640 "${stage}/leaf.crt" "${ETC_DIR}/leaf.crt"
    install -o root -g home-location -m 0640 "${stage}/leaf.key" "${ETC_DIR}/leaf.key"
    rm -rf -- "${stage}"
}

validate_certificate_set() {
    local ca_crt="$1" ca_der="$2" leaf_crt="$3" leaf_key="$4" stage
    stage="$(mktemp -d)"
    register_temp_dir "${stage}"
    openssl verify -CAfile "${ca_crt}" "${leaf_crt}" >/dev/null 2>&1 || return 1
    openssl x509 -checkend 2592000 -noout -in "${ca_crt}" >/dev/null 2>&1 || return 1
    openssl x509 -checkend 2592000 -noout -in "${leaf_crt}" >/dev/null 2>&1 || return 1
    openssl x509 -checkhost gs-loc.apple.com -noout -in "${leaf_crt}" >/dev/null 2>&1 \
        || return 1
    openssl x509 -checkhost gs-loc-cn.apple.com -noout -in "${leaf_crt}" >/dev/null 2>&1 \
        || return 1
    openssl x509 -checkhost gspe85-ssl.ls.apple.com -noout -in "${leaf_crt}" >/dev/null 2>&1 \
        || return 1
    openssl x509 -checkhost gspe85-9-cn-ssl.ls.apple.com -noout -in "${leaf_crt}" >/dev/null 2>&1 \
        || return 1
    openssl x509 -in "${ca_crt}" -outform DER -out "${stage}/ca.der" \
        >/dev/null 2>&1 || return 1
    cmp -s "${stage}/ca.der" "${ca_der}" || return 1
    openssl pkey -in "${leaf_key}" -pubout -outform DER -out "${stage}/key.pub" \
        >/dev/null 2>&1 || return 1
    openssl x509 -in "${leaf_crt}" -pubkey -noout 2>/dev/null \
        | openssl pkey -pubin -outform DER -out "${stage}/cert.pub" \
            >/dev/null 2>&1 || return 1
    cmp -s "${stage}/key.pub" "${stage}/cert.pub"
}

render_ca_profile() {
    note "Rendering the deterministic iOS CA profile"
    PYTHONPATH="${SOURCE_DIR}/src" python3 -m home_location_endpoint.profile \
        --ca-der "${ETC_DIR}/ca.der" \
        --output "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
    chown root:root "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
    chmod 0644 "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
}

load_or_create_credentials() {
    local derived_output derived_public key_output
    if [[ -f "${ETC_DIR}/install.env" ]]; then
        # shellcheck disable=SC1091
        source "${ETC_DIR}/install.env"
        [[ "${HLE_PROXY_PROTOCOL:-vless-reality}" == "${PROXY_PROTOCOL}" ]] \
            || die "changing the proxy protocol in place is not supported"
        if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
            [[ -n "${HLE_UUID:-}" && -n "${HLE_PRIVATE_KEY:-}" \
              && -n "${HLE_PUBLIC_KEY:-}" && -n "${HLE_SHORT_ID:-}" ]] \
                || die "existing VLESS + REALITY credentials are incomplete"
            CLIENT_UUID="${HLE_UUID}"
            PRIVATE_KEY="${HLE_PRIVATE_KEY}"
            PUBLIC_KEY="${HLE_PUBLIC_KEY}"
            SHORT_ID="${HLE_SHORT_ID}"
            derived_output="$(/usr/local/bin/xray x25519 -i "${PRIVATE_KEY}")"
            derived_public="$(printf '%s\n' "${derived_output}" | awk -F': *' \
                '/^(Password \(PublicKey\)|Password|PublicKey|Public key):/{print $2; exit}')"
            [[ -n "${derived_public}" && "${derived_public}" == "${PUBLIC_KEY}" ]] \
                || die "existing REALITY public/private key pair does not match"
        else
            SS_PASSWORD="${HLE_SS_PASSWORD:-}"
            PYTHONPATH="${SOURCE_DIR}/src" python3 -c \
                'import sys; from home_location_endpoint.render import validate_ss2022_password; validate_ss2022_password(sys.argv[1])' \
                "${SS_PASSWORD}" || die "existing SS2022 password is invalid"
        fi
        return
    fi
    if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
        CLIENT_UUID="$(/usr/local/bin/xray uuid)"
        key_output="$(/usr/local/bin/xray x25519)"
        PRIVATE_KEY="$(printf '%s\n' "${key_output}" | awk -F': *' '/^(PrivateKey|Private key):/{print $2; exit}')"
        PUBLIC_KEY="$(printf '%s\n' "${key_output}" | awk -F': *' '/^(Password \(PublicKey\)|Password|PublicKey|Public key):/{print $2; exit}')"
        SHORT_ID="$(openssl rand -hex 8)"
        [[ -n "${CLIENT_UUID}" && -n "${PRIVATE_KEY}" && -n "${PUBLIC_KEY}" && -n "${SHORT_ID}" ]] \
            || die "could not parse generated Xray credentials"
    else
        SS_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
        PYTHONPATH="${SOURCE_DIR}/src" python3 -c \
            'import sys; from home_location_endpoint.render import validate_ss2022_password; validate_ss2022_password(sys.argv[1])' \
            "${SS_PASSWORD}" || die "could not generate a valid SS2022 password"
    fi
}

render_and_validate() {
    local stage
    local -a render_args
    note "Rendering and validating the endpoint configuration"
    stage="$(mktemp -d)"
    register_temp_dir "${stage}"
    # Use --opt=value form for every operator/generated string value. REALITY
    # x25519 keys are base64url and can legitimately begin with '-', which the
    # space-separated form makes argparse reject as an option ("expected one
    # argument"). The '=' form is unambiguous regardless of a leading dash.
    render_args=(
        --config "${stage}/config.json"
        --uri "${stage}/node-uri.txt"
        --server="${SERVER}" --port "${PORT}"
        --protocol "${PROXY_PROTOCOL}"
        --listen="${LISTEN_ADDRESS}"
    )
    if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
        render_args+=(
            --uuid="${CLIENT_UUID}"
            --reality-sni="${REALITY_SNI}" --reality-target="${REALITY_TARGET}"
            --private-key="${PRIVATE_KEY}" --public-key="${PUBLIC_KEY}"
            --short-id="${SHORT_ID}"
            --fallback-upload-after "${FALLBACK_UPLOAD_AFTER}"
            --fallback-upload-rate "${FALLBACK_UPLOAD_RATE}"
            --fallback-upload-burst "${FALLBACK_UPLOAD_BURST}"
            --fallback-download-after "${FALLBACK_DOWNLOAD_AFTER}"
            --fallback-download-rate "${FALLBACK_DOWNLOAD_RATE}"
            --fallback-download-burst "${FALLBACK_DOWNLOAD_BURST}"
        )
    else
        render_args+=(--ss-password="${SS_PASSWORD}")
    fi
    python3 "${SOURCE_DIR}/src/home_location_endpoint/render.py" "${render_args[@]}"
    /usr/local/bin/xray run -test -config "${stage}/config.json"
    install -o root -g xray -m 0640 "${stage}/config.json" "${XRAY_CONFIG_DIR}/config.json"
    install -o root -g root -m 0600 "${stage}/node-uri.txt" "${ETC_DIR}/node-uri.txt"

    umask 0077
    {
        printf 'HLE_MODE=%q\n' "${MODE}"
        printf 'HLE_PORT=%q\n' "${PORT}"
        printf 'HLE_SERVER=%q\n' "${SERVER}"
        printf 'HLE_SERVER_EXPLICIT=%q\n' "${SERVER_EXPLICIT}"
        printf 'HLE_CREATED_HOME_USER=%q\n' "${CREATED_HOME_USER}"
        printf 'HLE_CREATED_HOME_GROUP=%q\n' "${CREATED_HOME_GROUP}"
        printf 'HLE_CREATED_XRAY_USER=%q\n' "${CREATED_XRAY_USER}"
        printf 'HLE_CREATED_XRAY_GROUP=%q\n' "${CREATED_XRAY_GROUP}"
        printf 'HLE_CREATED_BOT_USER=%q\n' "${CREATED_BOT_USER}"
        printf 'HLE_CREATED_BOT_GROUP=%q\n' "${CREATED_BOT_GROUP}"
        printf 'HLE_PROXY_PROTOCOL=%q\n' "${PROXY_PROTOCOL}"
        if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
            printf 'HLE_REALITY_SNI=%q\n' "${REALITY_SNI}"
            printf 'HLE_REALITY_TARGET=%q\n' "${REALITY_TARGET}"
            printf 'HLE_UUID=%q\n' "${CLIENT_UUID}"
            printf 'HLE_PRIVATE_KEY=%q\n' "${PRIVATE_KEY}"
            printf 'HLE_PUBLIC_KEY=%q\n' "${PUBLIC_KEY}"
            printf 'HLE_SHORT_ID=%q\n' "${SHORT_ID}"
        else
            printf 'HLE_SS_PASSWORD=%q\n' "${SS_PASSWORD}"
        fi
        printf 'HLE_LISTEN_ADDRESS=%q\n' "${LISTEN_ADDRESS}"
        printf 'HLE_FALLBACK_UPLOAD_AFTER=%q\n' "${FALLBACK_UPLOAD_AFTER}"
        printf 'HLE_FALLBACK_UPLOAD_RATE=%q\n' "${FALLBACK_UPLOAD_RATE}"
        printf 'HLE_FALLBACK_UPLOAD_BURST=%q\n' "${FALLBACK_UPLOAD_BURST}"
        printf 'HLE_FALLBACK_DOWNLOAD_AFTER=%q\n' "${FALLBACK_DOWNLOAD_AFTER}"
        printf 'HLE_FALLBACK_DOWNLOAD_RATE=%q\n' "${FALLBACK_DOWNLOAD_RATE}"
        printf 'HLE_FALLBACK_DOWNLOAD_BURST=%q\n' "${FALLBACK_DOWNLOAD_BURST}"
    } > "${ETC_DIR}/install.env"
    chmod 0600 "${ETC_DIR}/install.env"
    printf 'mode=%s\nxray=%s\nprotocol=%s\n' \
        "${MODE}" "${XRAY_VERSION}" "${PROXY_PROTOCOL}" > "${MARKER}"
    chmod 0600 "${MARKER}"
}

write_common_mode_state() {
    printf '%s\n' "${MODE}" > "${ETC_DIR}/mode"
    chmod 0644 "${ETC_DIR}/mode"
    if [[ "${MODE}" == "modifier-only" ]]; then
        umask 0077
        {
            printf 'HLE_MODE=%q\n' "${MODE}"
            printf 'HLE_SERVER=%q\n' "${SERVER}"
            printf 'HLE_SERVER_EXPLICIT=%q\n' "${SERVER_EXPLICIT}"
            printf 'HLE_CREATED_HOME_USER=%q\n' "${CREATED_HOME_USER}"
            printf 'HLE_CREATED_HOME_GROUP=%q\n' "${CREATED_HOME_GROUP}"
            printf 'HLE_CREATED_XRAY_USER=%q\n' 0
            printf 'HLE_CREATED_XRAY_GROUP=%q\n' 0
        } > "${ETC_DIR}/install.env"
        chmod 0600 "${ETC_DIR}/install.env"
        printf 'mode=%s\n' "${MODE}" > "${MARKER}"
        chmod 0600 "${MARKER}"
    fi
}

normalize_managed_permissions() {
    chown root:home-location "${ETC_DIR}"
    chmod 0750 "${ETC_DIR}"
    chown root:root \
        "${ETC_DIR}/mode" "${ETC_DIR}/install.env" "${MARKER}" \
        "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der" \
        "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
    chmod 0644 "${ETC_DIR}/mode" "${ETC_DIR}/ca.crt" "${ETC_DIR}/ca.der" \
        "${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig"
    chmod 0600 "${ETC_DIR}/install.env" "${MARKER}"
    chown root:home-location "${STATE_DIR}"
    chmod 0750 "${STATE_DIR}"
    chown root:root "${STATE_DIR}/modifier.state"
    chmod 0644 "${STATE_DIR}/modifier.state"
    chown root:home-location \
        "${ETC_DIR}/location.json" "${ETC_DIR}/jitter.seed" \
        "${ETC_DIR}/leaf.crt" "${ETC_DIR}/leaf.key"
    chmod 0640 \
        "${ETC_DIR}/location.json" "${ETC_DIR}/jitter.seed" \
        "${ETC_DIR}/leaf.crt" "${ETC_DIR}/leaf.key"
    if proxy_mode; then
        chown root:root "${ETC_DIR}/node-uri.txt"
        chmod 0600 "${ETC_DIR}/node-uri.txt"
    fi
    if advanced_mode; then
        chown root:root "${ETC_DIR}/runtime.env"
        chmod 0644 "${ETC_DIR}/runtime.env"
        chown root:home-location-bot "${ETC_DIR}/telegram"
        chmod 0750 "${ETC_DIR}/telegram"
        chown root:home-location-bot \
            "${ETC_DIR}/telegram/token" "${ETC_DIR}/telegram/chat_id"
        chmod 0640 "${ETC_DIR}/telegram/token" "${ETC_DIR}/telegram/chat_id"
        chown home-location-bot:home-location "${STATE_DIR}/control"
        chmod 0750 "${STATE_DIR}/control"
        chown home-location-bot:home-location \
            "${STATE_DIR}/control/location.json" \
            "${STATE_DIR}/control/modifier.state"
        chmod 0640 \
            "${STATE_DIR}/control/location.json" \
            "${STATE_DIR}/control/modifier.state"
    fi
}

install_services() {
    note "Installing and starting the scoped location service"
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/systemd/home-location-endpoint.service" \
        /etc/systemd/system/home-location-endpoint.service
    install -o root -g root -m 0644 \
        "${SOURCE_DIR}/configs/home-location-endpoint.logrotate" \
        /etc/logrotate.d/home-location-endpoint
    if proxy_mode; then
        install -o root -g root -m 0644 \
            "${SOURCE_DIR}/systemd/xray.service" \
            /etc/systemd/system/xray.service
    fi
    if advanced_mode; then
        install -o root -g root -m 0644 \
            "${SOURCE_DIR}/systemd/home-location-telegram-bot.service" \
            /etc/systemd/system/home-location-telegram-bot.service
    fi
    systemctl daemon-reload
    systemctl enable home-location-endpoint.service >/dev/null
    systemctl restart home-location-endpoint.service \
        || die "the location interceptor failed to start; inspect journalctl -u home-location-endpoint"
    if ! proxy_mode; then
        return
    fi
    systemctl enable xray.service >/dev/null
    systemctl restart xray.service \
        || die "Xray failed to start; the installation transaction will be rolled back"
    if advanced_mode; then
        systemctl enable home-location-telegram-bot.service >/dev/null
        rm -f /run/home-location-endpoint-bot/health
        systemctl restart home-location-telegram-bot.service \
            || die "the Telegram controller failed to start; the installation transaction will be rolled back"
        local attempt
        for ((attempt = 0; attempt < 200; attempt++)); do
            [[ -s /run/home-location-endpoint-bot/health ]] && break
            systemctl is-active --quiet home-location-telegram-bot.service \
                || die "the Telegram controller exited before becoming ready"
            sleep 0.2
        done
        [[ -s /run/home-location-endpoint-bot/health ]] \
            || die "the Telegram controller could not complete a Bot API long poll; another controller may be using this token, or Telegram is unreachable"
    fi
}

open_active_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
        note "Allowing TCP ${PORT} in the already-active UFW policy"
        if ! ufw allow "${PORT}/tcp" comment "Home Location Endpoint" >/dev/null; then
            printf 'WARNING: UFW is active but its proxy-port rule could not be added.\n' >&2
        fi
        if [[ "${PROXY_PROTOCOL}" == "ss2022" ]]; then
            if ! ufw allow "${PORT}/udp" comment "Home Location Endpoint SS2022" >/dev/null; then
                printf 'WARNING: UFW is active but its SS2022 UDP rule could not be added.\n' >&2
            fi
        fi
        if [[ -n "${PREVIOUS_PORT}" && "${PREVIOUS_PORT}" != "${PORT}" ]]; then
            printf 'WARNING: the proxy port changed from %s to %s; review and remove the old UFW rule if it was project-created.\n' \
                "${PREVIOUS_PORT}" "${PORT}" >&2
        fi
    else
        printf 'Firewall note: UFW was not active, so the installer did not change firewall state.\n'
    fi
}

show_result() {
    local city fingerprint server_label protocol_label location_file mode_completion_zh
    location_file="${ETC_DIR}/location.json"
    advanced_mode && location_file="${STATE_DIR}/control/location.json"
    city="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["source"]["city"]+", "+d["source"]["country_code"])' "${location_file}")"
    fingerprint="$(openssl x509 -in "${ETC_DIR}/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
    if proxy_mode; then
        if [[ "${SERVER_EXPLICIT}" -eq 1 ]]; then
            server_label="${SERVER} (from --server)"
        else
            server_label="${SERVER} (auto-detected egress IP)"
        fi
        if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
            protocol_label="VLESS + REALITY + Vision"
        else
            protocol_label="Shadowsocks 2022 (2022-blake3-aes-256-gcm)"
        fi
        mode_completion_zh="完整模式安装完成。"
        advanced_mode && mode_completion_zh="进阶模式安装完成。"
        cat <<EOF

${PROJECT} is ready in ${MODE} mode.
${PROJECT} ${mode_completion_zh}

Random location city / 随机定位城市: ${city}
Proxy protocol / 代理协议: ${protocol_label}
Server address in URI / 节点链接服务器地址: ${server_label}
Proxy URI / 代理节点链接:
$(cat "${ETC_DIR}/node-uri.txt")

iOS CA profile / iOS CA 描述文件: ${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig
CA SHA-256 / CA SHA-256 指纹: ${fingerprint}
Temporary phone download starts automatically below.
下方将自动启动手机临时下载。
EOF
        if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
            printf 'REALITY SNI / REALITY 伪装域名: %s\n' "${REALITY_SNI}"
        fi
        if advanced_mode; then
            cat <<EOF
Telegram location controller / Telegram 定位控制: active
Authorized chat / 授权 Chat ID: ${TELEGRAM_CHAT_ID}
Open the bot and send /menu to switch, add, delete, or restore locations.
打开 Bot 并发送 /menu，即可切换、增加、删除地点或恢复真实定位。
EOF
        fi
        if [[ "${SERVER_EXPLICIT}" -eq 0 ]]; then
            cat <<EOF

IMPORTANT: the URI address above is this host's auto-detected egress IP. If clients
reach this host through a Realm front, NAT, or a different ingress IP, that address
will not connect -- reinstall with --server <ingress-address> and keep the port identical.

重要：上方节点链接使用的是本机自动探测到的出口 IP。如果客户端通过 Realm 前置机、NAT
或其他入口 IP 接入，该地址将无法连接；请使用 --server <入口地址> 重新安装，并保持端口一致。
EOF
        fi
        cat <<EOF

If this landing server is behind one or more relays, use Realm pure TCP forwarding.
Keep the entry port and all client credentials unchanged at every relay.
如果落地机前面还有一级或多级中转，请使用 Realm 纯 TCP 转发。
每一级中转都必须保持入口端口和客户端凭据不变。

Next / 下一步:
  1. Copy the profile to the iPhone, install it, then enable full trust for this CA.
     将描述文件传到 iPhone 安装，然后为该 CA 开启完全信任。
  2. Import the proxy URI into a full-tunnel client and connect through this endpoint.
     将代理节点链接导入全局代理客户端，并通过本节点连接。
  3. Run 'sudo hle verify' and 'sudo hle status' for local checks.
     运行 'sudo hle verify' 和 'sudo hle status' 检查本机状态。
  4. Use Telegram /menu in advanced mode; beginner mode can use 'sudo hle relocate'.
     进阶模式使用 Telegram /menu；新手模式可运行 'sudo hle relocate'。
  5. Run 'sudo hle pause' to return real Apple responses; use 'sudo hle resume' to rewrite again.
     需要暂停定位修改时运行 'sudo hle pause'；再次运行 'sudo hle resume' 恢复改写。

Remove everything later with: sudo hle uninstall
以后如需完整卸载，运行：sudo hle uninstall
SSH was not changed. If a provider firewall exists, allow TCP ${PORT} there.
安装器未修改 SSH。如服务商另有云防火墙，请在其中放行 TCP ${PORT}。
EOF
        if [[ "${PROXY_PROTOCOL}" == "ss2022" ]]; then
            cat <<EOF
SS2022 native UDP also requires UDP ${PORT} in the provider firewall.
SS2022 原生 UDP 还需要在服务商防火墙中放行 UDP ${PORT}。
EOF
        fi
        return
    fi
    cat <<EOF

${PROJECT} is ready in modifier-only mode.
${PROJECT} 仅定位修改器模式安装完成。

Random location city / 随机定位城市: ${city}
iOS CA profile / iOS CA 描述文件: ${ETC_DIR}/Home-Location-Endpoint-CA.mobileconfig
CA SHA-256 / CA SHA-256 指纹: ${fingerprint}
Temporary phone download starts automatically below.
下方将自动启动手机临时下载。
Loopback interceptor / 本机定位拦截器: 127.0.0.1:10451
Xray integration example / Xray 接入示例: ${ETC_DIR}/xray-location-routing.example.json

Next / 下一步:
  1. Copy the profile to the iPhone, install it, then enable full trust for this CA.
     将描述文件传到 iPhone 安装，然后为该 CA 开启完全信任。
  2. Merge the example outbounds/routing rules into your own proxy configuration.
     将示例中的出站和路由规则合并到你自己的代理配置。
  3. Enable TLS/HTTP sniffing with routeOnly on the inbound that carries phone traffic.
     在承载手机流量的入站上启用 TLS/HTTP sniffing，并设置 routeOnly。
  4. Run 'sudo hle verify', then test that only the documented Apple hosts reach loopback:10451.
     运行 'sudo hle verify'，并确认只有文档列出的 Apple 域名进入 127.0.0.1:10451。
  5. Use 'sudo hle pause' and 'sudo hle resume' to switch rewriting off and on without a restart.
     使用 'sudo hle pause' 和 'sudo hle resume' 即时暂停或恢复改写，无需重启。

Remove everything this mode installed (it never touches your proxy core) with: sudo hle uninstall
完整卸载本模式安装的内容（不会触碰你的代理核心）：sudo hle uninstall
No proxy core, proxy port, firewall rule, or TCP tuning was installed in this mode.
本模式没有安装代理核心、代理端口、防火墙规则或 TCP 调优。
EOF
}

interactive_output() {
    [[ -t 1 ]]
}

serve_profile_download() {
    /usr/local/sbin/hle profile serve
}

auto_serve_profile() {
    if ! interactive_output; then
        cat <<'EOF'

Non-interactive output detected; the temporary CA download was not started.
检测到非交互式输出；未自动启动 CA 临时下载服务。
Run later / 稍后运行: sudo hle profile serve
EOF
        return 0
    fi

    cat <<'EOF'

Starting the one-time CA profile download now. It closes after one successful
download or 100 minutes. Press Ctrl+C to close it without affecting the installation.
正在启动一次性 CA 描述文件下载。首次成功下载或 100 分钟后自动关闭；
按 Ctrl+C 可提前关闭，不会影响已经完成的安装。
EOF
    local handoff_status
    if serve_profile_download; then
        return 0
    else
        handoff_status=$?
    fi

    case "${handoff_status}" in
        130|143)
            cat <<'EOF'
Temporary CA download closed by the operator. The endpoint remains installed and active.
CA 临时下载已由用户关闭；节点仍已完成安装并保持运行。
EOF
            ;;
        *)
            cat >&2 <<'EOF'
WARNING: the endpoint installation is complete, but the temporary CA download
could not start. Run it again later with: sudo hle profile serve
警告：节点安装已经完成，但 CA 临时下载未能启动。请稍后重新运行：
sudo hle profile serve
EOF
            ;;
    esac
}

main() {
    show_help_if_requested "$@"
    require_root
    bootstrap_if_needed "$@"
    acquire_install_lock
    preflight_common_state
    load_existing_settings
    parse_args "$@"
    select_install_mode
    check_os
    check_existing_installation
    check_resources
    install_packages
    select_advanced_options
    validate_explicit_overrides
    begin_transaction
    if proxy_mode; then
        detect_listen_address
        check_port_available
        install_xray
        install_baseline
    fi
    create_accounts_and_directories
    select_random_location
    prepare_advanced_control
    generate_certificates
    render_ca_profile
    if proxy_mode; then
        if [[ "${PROXY_PROTOCOL}" == "vless-reality" ]]; then
            validate_fixed_reality_target
            generate_fallback_limits
        fi
        load_or_create_credentials
        render_and_validate
    fi
    write_common_mode_state
    normalize_managed_permissions
    install_services
    /usr/local/sbin/hle verify
    TRANSACTION_COMMITTED=1
    if proxy_mode; then
        apply_baseline
        open_active_firewall
    fi
    show_result
    auto_serve_profile
}

if [[ "${HLE_SOURCE_ONLY:-0}" != "1" ]]; then
    main "$@"
fi
