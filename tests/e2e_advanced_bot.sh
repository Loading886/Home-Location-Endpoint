#!/usr/bin/env bash
set -Eeuo pipefail

API_ROOT="${HLE_TEST_TELEGRAM_API_ROOT:-http://127.0.0.1:19090}"
CHAT_ID="${HLE_TEST_TELEGRAM_CHAT_ID:-987654321}"
LOCATION=/var/lib/home-location-endpoint/control/location.json
MODIFIER=/var/lib/home-location-endpoint/control/modifier.state

[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f "${LOCATION}" && -f "${MODIFIER}" ]] || {
    echo "advanced mode is not installed" >&2
    exit 1
}

INITIAL_PRESETS="$(python3 - "${LOCATION}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(sorted(data["presets"]), separators=(",", ":")))
PY
)"
INITIAL_COUNT="$(python3 - "${LOCATION}" <<'PY'
import json
import sys

print(len(json.load(open(sys.argv[1], encoding="utf-8"))["presets"]))
PY
)"

inject() {
    local kind="$1" value="$2" update_id
    UPDATE_ID=$((UPDATE_ID + 1))
    update_id="${UPDATE_ID}"
    python3 - "${API_ROOT}/inject" "${CHAT_ID}" \
        "${kind}" "${update_id}" "${value}" <<'PY'
import json
import sys
import urllib.request

url, chat_id, kind, update_id, value = sys.argv[1:]
if kind == "callback":
    update = {
        "update_id": int(update_id),
        "callback_query": {
            "id": "callback-%s" % update_id,
            "data": value,
            "message": {"chat": {"id": int(chat_id)}},
        },
    }
else:
    update = {
        "update_id": int(update_id),
        "message": {"chat": {"id": int(chat_id)}, "text": value},
    }
request = urllib.request.Request(
    url,
    data=json.dumps(update, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    if json.load(response).get("ok") is not True:
        raise SystemExit("update injection failed")
PY
}

wait_for() {
    local expression="$1"
    for _ in $(seq 1 50); do
        if python3 - "${LOCATION}" "${MODIFIER}" "${expression}" <<'PY'
import json
import sys

location = json.load(open(sys.argv[1], encoding="utf-8"))
modifier = open(sys.argv[2], encoding="ascii").read().strip()
raise SystemExit(0 if eval(
    sys.argv[3], {"__builtins__": {}},
    {"location": location, "modifier": modifier, "any": any}
) else 1)
PY
        then
            return 0
        fi
        sleep 0.2
    done
    echo "timed out waiting for: ${expression}" >&2
    exit 1
}

UPDATE_ID="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

inject callback loc:set:tokyo
wait_for 'location["active"] == "tokyo" and modifier == "active"'

inject callback loc:restore
wait_for 'modifier == "paused"'

inject callback loc:add
inject message '🧪 Test Point'
inject message 'Automated integration test point'
inject message '40.7128, -74.0060'
inject callback loc:add-confirm
wait_for "any(k.startswith(\"custom_\") and k not in ${INITIAL_PRESETS} for k in location[\"presets\"])"

CUSTOM_KEY="$(python3 - "${LOCATION}" "${INITIAL_PRESETS}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
initial = set(json.loads(sys.argv[2]))
print(next(
    key for key in data["presets"]
    if key.startswith("custom_") and key not in initial
))
PY
)"
inject callback "loc:delete:${CUSTOM_KEY}"
inject callback "loc:delete-confirm:${CUSTOM_KEY}"
wait_for "\"${CUSTOM_KEY}\" not in location[\"presets\"]"

inject callback loc:set:ip_city
wait_for 'location["active"] == "ip_city" and modifier == "active"'

[[ "$(python3 - "${LOCATION}" <<'PY'
import json
import sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["presets"]))
PY
)" == "${INITIAL_COUNT}" ]] || { echo "unexpected final preset count" >&2; exit 1; }

hle verify >/dev/null
if runuser -u home-location-bot -- cat \
    /etc/home-location-endpoint/install.env >/dev/null 2>&1; then
    echo "Telegram bot account can read install.env" >&2
    exit 1
fi

printf 'Advanced Telegram location workflow: OK\n'
