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

printf 'installer mode tests: OK\n'
