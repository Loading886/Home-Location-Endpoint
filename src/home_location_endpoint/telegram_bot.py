#!/usr/bin/env python3
"""Single-operator Telegram location controller for advanced installs."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows unit-test environment; production targets Linux.
    fcntl = None

try:
    import preset_manager as presets
except ImportError:  # package import during tests and installer validation
    from . import preset_manager as presets


TOKEN_RE = re.compile(r"^[0-9]{6,20}:[A-Za-z0-9_-]{20,100}$")
CHAT_RE = re.compile(r"^-?[0-9]{5,20}$")
TOKEN_FILE = Path(os.environ.get(
    "HLE_TELEGRAM_TOKEN_FILE", "/etc/home-location-endpoint/telegram/token"
))
CHAT_FILE = Path(os.environ.get(
    "HLE_TELEGRAM_CHAT_FILE", "/etc/home-location-endpoint/telegram/chat_id"
))
LOCATION_FILE = Path(os.environ.get(
    "HLE_LOCATION_CONFIG", "/var/lib/home-location-endpoint/control/location.json"
))
MODIFIER_FILE = Path(os.environ.get(
    "HLE_MODIFIER_STATE", "/var/lib/home-location-endpoint/control/modifier.state"
))
BACKUP_DIR = Path(os.environ.get(
    "HLE_LOCATION_BACKUPS", "/var/backups/home-location-endpoint"
))
OFFSET_FILE = LOCATION_FILE.parent / "telegram.offset"
LOCK_FILE = LOCATION_FILE.parent / "telegram.lock"
HEALTH_FILE = Path(os.environ.get(
    "HLE_TELEGRAM_HEALTH_FILE", "/run/home-location-endpoint-bot/health"
))
MAX_RESPONSE = 2 * 1024 * 1024
SESSION_TTL = 900
BOT_COMMANDS = [
    {"command": "menu", "description": "打开定位菜单"},
    {"command": "status", "description": "查看当前定位"},
]
BUILTIN_MENU_LABELS = {
    "ip_city": "🌐 出口城市",
    "los_angeles": "🇺🇸 洛杉矶",
    "tokyo": "🇯🇵 东京",
    "hong_kong": "🇭🇰 香港",
    "singapore": "🇸🇬 新加坡",
    "kuala_lumpur": "🇲🇾 吉隆坡",
    "paris": "🇫🇷 巴黎",
    "frankfurt": "🇩🇪 法兰克福",
    "reykjavik": "🇮🇸 雷克雅未克",
    "kunlun_station": "🇦🇶 南极昆仑站",
}


class BotError(RuntimeError):
    pass


def validate_token(value):
    value = str(value or "").strip()
    if not TOKEN_RE.fullmatch(value):
        raise BotError("Telegram bot token format is invalid")
    return value


def validate_chat_id(value):
    value = str(value or "").strip()
    if not CHAT_RE.fullmatch(value):
        raise BotError("Telegram chat ID format is invalid")
    return value


def telegram_api_root():
    value = os.environ.get("HLE_TELEGRAM_API_ROOT", "https://api.telegram.org")
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BotError("Telegram API root is invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise BotError("Telegram API root has an invalid port") from exc
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        host = parsed.hostname.lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise BotError("plain HTTP Telegram API is allowed only on loopback")
    else:
        raise BotError("Telegram API root must use HTTPS")
    return value.strip().rstrip("/")


class Telegram:
    def __init__(self, token):
        self.token = validate_token(token)
        self.base = "%s/bot%s/" % (telegram_api_root(), self.token)

    def call(self, method, payload=None, timeout=35):
        encoded = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base + method,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
        except (OSError, urllib.error.HTTPError) as exc:
            raise BotError("Telegram API request failed") from exc
        if len(body) > MAX_RESPONSE:
            raise BotError("Telegram API response is too large")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BotError("Telegram API returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = str(result.get("description", "request rejected"))[:200]
            raise BotError("Telegram API: %s" % description)
        return result.get("result")


def validate_credentials(token, chat_id):
    chat_id = validate_chat_id(chat_id)
    api = Telegram(token)
    identity = api.call("getMe")
    if not isinstance(identity, dict) or not identity.get("is_bot"):
        raise BotError("token does not identify a Telegram bot")
    chat = api.call("getChat", {"chat_id": chat_id})
    if not isinstance(chat, dict) or str(chat.get("id")) != chat_id:
        raise BotError("chat ID is not accessible to this bot; send /start first")
    webhook = api.call("getWebhookInfo")
    if not isinstance(webhook, dict):
        raise BotError("Telegram returned invalid webhook information")
    if str(webhook.get("url") or "").strip():
        raise BotError("this bot already has a webhook; create a dedicated bot")
    username = str(identity.get("username") or identity.get("first_name") or "bot")
    print("Telegram bot verified: @%s" % username.lstrip("@"))


def _button_style(button):
    """Use the same blue/green/red semantics as the Apple Relay bot."""
    text = str(button.get("text") or "").strip()
    callback = str(button.get("callback_data") or "")
    if text.startswith(("✓", "✅")):
        return "success"
    if callback == "loc:delete-menu" or callback.startswith((
        "loc:delete:", "loc:delete-confirm:", "loc:cancel",
    )):
        return "danger"
    if callback in {"loc:add", "loc:add-confirm"}:
        return "success"
    return "primary"


def keyboard(rows):
    styled_rows = []
    for row in rows:
        styled_row = []
        for button in row:
            styled_button = dict(button)
            styled_button.setdefault("style", _button_style(styled_button))
            styled_row.append(styled_button)
        styled_rows.append(styled_row)
    return json.dumps(
        {"inline_keyboard": styled_rows},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class LocationBot:
    def __init__(self, token, chat_id):
        self.api = Telegram(token)
        self.chat_id = validate_chat_id(chat_id)
        self.session = None
        self.offset = self._load_offset()

    def _load_offset(self):
        try:
            value = int(OFFSET_FILE.read_text(encoding="ascii").strip())
            return max(0, value)
        except (OSError, ValueError):
            return 0

    def _save_offset(self):
        temporary = OFFSET_FILE.with_name(".%s.%d" % (OFFSET_FILE.name, os.getpid()))
        temporary.write_text("%d\n" % self.offset, encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, OFFSET_FILE)

    def _write_health(self):
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEALTH_FILE.with_name(".%s.%d" % (HEALTH_FILE.name, os.getpid()))
        temporary.write_text("%.6f\n" % time.time(), encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, HEALTH_FILE)

    @contextmanager
    def locked(self):
        with LOCK_FILE.open("a+", encoding="ascii") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def send(self, text, rows=None):
        payload = {"chat_id": self.chat_id, "text": text[:4000]}
        if rows is not None:
            payload["reply_markup"] = keyboard(rows)
        self.api.call("sendMessage", payload)

    def answer(self, callback_id, text=""):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:180]
        self.api.call("answerCallbackQuery", payload)

    @staticmethod
    def _label(entry, key=None):
        value = BUILTIN_MENU_LABELS.get(
            key,
            str(entry.get("menu_label") or entry.get("label") or "Location"),
        )
        return value if len(value) <= 40 else value[:39] + "…"

    def setup_ui(self):
        self.api.call("setMyCommands", {
            "commands": json.dumps(BOT_COMMANDS, ensure_ascii=False),
        })
        self.api.call("setChatMenuButton", {
            "chat_id": self.chat_id,
            "menu_button": json.dumps({"type": "commands"}),
        })

    def show_menu(self, notice=None):
        data = presets.load(LOCATION_FILE)
        state = MODIFIER_FILE.read_text(encoding="ascii").strip()
        rows = []
        buttons = []
        for key, entry in data["presets"].items():
            label = self._label(entry, key)
            if state == "active" and key == data["active"]:
                label = "✓ " + label
            buttons.append({"text": label, "callback_data": "loc:set:%s" % key})
        rows.extend(buttons[index:index + 2] for index in range(0, len(buttons), 2))
        restore = "✓ 🌍 真实定位" if state == "paused" else "🌍 真实定位"
        rows.append([{"text": restore, "callback_data": "loc:restore"}])
        rows.append([
            {"text": "➕ 增加地点", "callback_data": "loc:add"},
            {"text": "➖ 删除地点", "callback_data": "loc:delete-menu"},
        ])
        current = data["presets"][data["active"]]
        current_label = self._label(current, data["active"])
        message = []
        if notice:
            message.append(notice)
        message.append("📍 Home-Location-Endpoint 定位控制")
        if state == "paused":
            message.append("当前：🌍 真实定位（位置改写已暂停）")
            message.append("保留地点：%s" % current_label)
        else:
            message.append("当前：%s" % current_label)
            message.append("地址：%s" % current.get("address", "未记录"))
        message.append("选择城市立即生效，不需要重启服务。")
        self.send("\n\n".join(message), rows)

    def show_delete_menu(self):
        data = presets.load(LOCATION_FILE)
        rows = []
        buttons = [
            {
                "text": "🗑 " + self._label(entry, key),
                "callback_data": "loc:delete:%s" % key,
            }
            for key, entry in data["presets"].items()
            if key != data["active"]
        ]
        rows.extend(buttons[index:index + 2] for index in range(0, len(buttons), 2))
        rows.append([{"text": "↩️ 返回", "callback_data": "loc:menu"}])
        self.send("选择要删除的地点。当前地点不会出现在删除列表中。", rows)

    def start_add(self):
        self.session = {"step": "label", "updated": time.monotonic()}
        self.send("增加地点 1/4：请发送简短名称，例如 🇺🇸 New York。", [
            [{"text": "✖️ 取消", "callback_data": "loc:cancel"}]
        ])

    def handle_text(self, text):
        if self.session and time.monotonic() - self.session["updated"] > SESSION_TTL:
            self.session = None
        if not self.session:
            parts = text.split()
            if not parts:
                return
            command = parts[0].split("@", 1)[0].lower()
            if command in {"/start", "/menu", "/location", "/status"}:
                self.show_menu()
            else:
                self.send("请使用 /menu 打开定位菜单。")
            return
        self.session["updated"] = time.monotonic()
        try:
            if self.session["step"] == "label":
                self.session["label"] = presets.validate_menu_label(text)
                self.session["step"] = "address"
                self.send("增加地点 2/4：请发送用于识别的地址。")
            elif self.session["step"] == "address":
                self.session["address"] = presets.validate_address(text)
                self.session["step"] = "coordinates"
                self.send("增加地点 3/4：请发送 WGS84 坐标，格式为 纬度, 经度。")
            elif self.session["step"] == "coordinates":
                lat, lon = presets.parse_coordinates(text)
                self.session.update({"lat": lat, "lon": lon, "step": "confirm"})
                self.send(
                    "增加地点 4/4：确认保存？\n\n%s\n%s\n%.6f, %.6f"
                    % (self.session["label"], self.session["address"], lat, lon),
                    [[
                        {"text": "✅ 保存", "callback_data": "loc:add-confirm"},
                        {"text": "✖️ 取消", "callback_data": "loc:cancel"},
                    ]],
                )
            else:
                self.send("当前正在等待按钮确认，或点击取消。")
        except presets.PresetError as exc:
            self.send("输入无效：%s\n请重新发送，或点击取消。" % exc)

    def handle_callback(self, callback):
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        self.answer(callback_id)
        if data == "loc:menu":
            self.show_menu()
        elif data == "loc:add":
            self.start_add()
        elif data == "loc:cancel":
            self.session = None
            self.show_menu("已取消。")
        elif data == "loc:restore":
            with self.locked():
                presets.write_modifier_state(MODIFIER_FILE, "paused")
            self.show_menu("已恢复真实定位。")
        elif data == "loc:delete-menu":
            self.show_delete_menu()
        elif data.startswith("loc:set:"):
            key = data[8:]
            with self.locked():
                entry, _saved = presets.set_active(LOCATION_FILE, BACKUP_DIR, key)
                presets.write_modifier_state(MODIFIER_FILE, "active")
            self.show_menu("已切换到 %s。" % self._label(entry, key))
        elif data.startswith("loc:delete:"):
            key = data[11:]
            current = presets.load(LOCATION_FILE)
            entry = current["presets"].get(key)
            if entry is None or key == current["active"]:
                self.show_menu("该地点不存在或仍是当前地点。")
                return
            self.send("确认删除 %s？" % self._label(entry, key), [[
                {"text": "🗑 确认删除", "callback_data": "loc:delete-confirm:%s" % key},
                {"text": "✖️ 取消", "callback_data": "loc:menu"},
            ]])
        elif data.startswith("loc:delete-confirm:"):
            key = data[19:]
            with self.locked():
                entry, _saved = presets.delete(LOCATION_FILE, BACKUP_DIR, key)
            self.show_menu("已删除 %s。" % self._label(entry, key))
        elif data == "loc:add-confirm":
            if not self.session or self.session.get("step") != "confirm":
                self.show_menu("增加地点会话已过期。")
                return
            with self.locked():
                key, saved = presets.add(
                    LOCATION_FILE,
                    BACKUP_DIR,
                    self.session["label"],
                    self.session["address"],
                    self.session["lat"],
                    self.session["lon"],
                )
            self.session = None
            self.show_menu("已增加 %s。" % self._label(saved["presets"][key], key))

    def process_update(self, update):
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) == self.chat_id:
                self.handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self.chat_id:
            return
        text = message.get("text")
        if isinstance(text, str):
            self.handle_text(text.strip())

    def run(self):
        self.api.call("deleteWebhook", {"drop_pending_updates": "false"})
        self.setup_ui()
        while True:
            try:
                updates = self.api.call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
                    timeout=40,
                )
                if not isinstance(updates, list):
                    raise BotError("getUpdates returned an invalid result")
                # Readiness means that long polling actually worked. Writing a
                # heartbeat before this point can hide a duplicate Bot token or
                # a controller that has no working path to Telegram.
                self._write_health()
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    try:
                        update_id = int(update.get("update_id", -1))
                    except (TypeError, ValueError):
                        continue
                    if update_id < self.offset:
                        continue
                    try:
                        self.process_update(update)
                    except (
                        BotError, OSError, presets.PresetError, ValueError,
                        KeyError, TypeError, IndexError,
                    ) as exc:
                        self.send("操作失败：%s" % str(exc)[:500])
                    self.offset = update_id + 1
                    self._save_offset()
            except BotError as exc:
                print("telegram poll error: %s" % exc, file=sys.stderr, flush=True)
                time.sleep(5)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-credentials", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.validate_credentials:
        validate_credentials(
            os.environ.get("HLE_TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("HLE_TELEGRAM_CHAT_ID", ""),
        )
        return
    token = TOKEN_FILE.read_text(encoding="ascii").strip()
    chat_id = CHAT_FILE.read_text(encoding="ascii").strip()
    LocationBot(token, chat_id).run()


if __name__ == "__main__":
    try:
        main()
    except (BotError, OSError, presets.PresetError, ValueError) as exc:
        raise SystemExit("home-location telegram bot: %s" % exc) from exc
