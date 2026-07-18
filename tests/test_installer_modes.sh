#!/usr/bin/env bash
# The tested functions intentionally mutate globals inside failure-isolating subshells.
# shellcheck disable=SC2030,SC2031
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HLE_SOURCE_ONLY=1
# shellcheck source=install.sh
source "${ROOT}/install.sh"

reset_mode_state() {
    MODE=""
    EXISTING_MODE=""
    MODE_EXPLICIT=0
    PROXY_OPTION_EXPLICIT=0
    SERVER_EXPLICIT=0
    CREATED_HOME_USER=0
    CREATED_HOME_GROUP=0
    CREATED_XRAY_USER=0
    CREATED_XRAY_GROUP=0
}

reset_mode_state
MODE="full"
select_install_mode
[[ "${MODE}" == "full" ]]

reset_mode_state
MODE="modifier-only"
select_install_mode
[[ "${MODE}" == "modifier-only" ]]

if (
    reset_mode_state
    MODE="modifier-only"
    PROXY_OPTION_EXPLICIT=1
    select_install_mode >/dev/null 2>&1
); then
    printf 'modifier-only accepted proxy-specific options\n' >&2
    exit 1
fi

if (
    reset_mode_state
    EXISTING_MODE="full"
    MODE="modifier-only"
    MODE_EXPLICIT=1
    select_install_mode >/dev/null 2>&1
); then
    printf 'installer accepted an in-place mode change\n' >&2
    exit 1
fi

temporary="$(mktemp -d)"
trap 'rm -rf "${temporary}"' EXIT
ETC_DIR="${temporary}/etc"
mkdir -p "${ETC_DIR}"
printf '%s\n' 'modifier-only' > "${ETC_DIR}/mode"
reset_mode_state
load_existing_settings
[[ "${EXISTING_MODE}" == "modifier-only" ]]
[[ "${MODE}" == "modifier-only" ]]

printf '%s\n' 'HLE_MODE=full' > "${ETC_DIR}/install.env"
if (
    reset_mode_state
    load_existing_settings >/dev/null 2>&1
); then
    printf 'installer accepted conflicting mode records\n' >&2
    exit 1
fi

rm -f "${ETC_DIR}/mode"
printf '%s\n' 'HLE_PORT=443' > "${ETC_DIR}/install.env"
reset_mode_state
load_existing_settings
[[ "${EXISTING_MODE}" == "full" ]]
[[ "${MODE}" == "full" ]]

printf '%s\n' \
    'HLE_MODE=full' \
    'HLE_SERVER_EXPLICIT=1' \
    'HLE_CREATED_HOME_USER=1' \
    'HLE_CREATED_HOME_GROUP=1' \
    'HLE_CREATED_XRAY_USER=0' \
    'HLE_CREATED_XRAY_GROUP=0' > "${ETC_DIR}/install.env"
reset_mode_state
load_existing_settings
[[ "${SERVER_EXPLICIT}" -eq 1 ]]
[[ "${CREATED_HOME_USER}" -eq 1 ]]
[[ "${CREATED_HOME_GROUP}" -eq 1 ]]
[[ "${CREATED_XRAY_USER}" -eq 0 ]]
[[ "${CREATED_XRAY_GROUP}" -eq 0 ]]

if (
    printf '%s\n' 'HLE_MODE=full' 'HLE_CREATED_HOME_USER=invalid' \
        > "${ETC_DIR}/install.env"
    reset_mode_state
    load_existing_settings >/dev/null 2>&1
); then
    printf 'installer accepted an invalid ownership inventory flag\n' >&2
    exit 1
fi

(
    interactive_output() { return 0; }
    handoff_called=0
    serve_profile_download() {
        handoff_called=1
        return 0
    }
    auto_serve_profile >/dev/null
    [[ "${handoff_called}" -eq 1 ]]
)

(
    interactive_output() { return 1; }
    handoff_called=0
    serve_profile_download() {
        handoff_called=1
        return 0
    }
    auto_serve_profile >/dev/null
    [[ "${handoff_called}" -eq 0 ]]
)

(
    interactive_output() { return 0; }
    serve_profile_download() { return 7; }
    auto_serve_profile >/dev/null 2>&1
)

(
    group_exists=0
    getent() {
        if [[ "$1" == "group" && "${group_exists}" -eq 1 ]]; then
            printf '%s\n' 'home-location:x:998:'
            return 0
        fi
        return 2
    }
    groupadd() { group_exists=1; }
    id() { return 1; }
    useradd() { return 0; }
    created_user=0
    created_group=0
    ensure_system_account \
        home-location home-location created_user created_group
    [[ "${created_user}" -eq 1 ]]
    [[ "${created_group}" -eq 1 ]]
)

printf 'installer mode tests: OK\n'
