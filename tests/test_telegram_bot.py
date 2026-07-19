import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from home_location_endpoint import location_picker, preset_manager, telegram_bot


TOKEN = "123456789:" + "A" * 35
CHAT_ID = "987654321"


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None, timeout=35):
        self.calls.append((method, payload or {}, timeout))
        return True

    def send_document(self, chat_id, filename, content, caption):
        self.calls.append(("sendDocument", {
            "chat_id": chat_id,
            "filename": filename,
            "content": content,
            "caption": caption,
        }, 45))
        return True


class TelegramBotTests(unittest.TestCase):
    def make_config(self):
        info = {
            "ip": "203.0.113.9",
            "city": "Test City",
            "region": "Test Region",
            "country": "Test Country",
            "country_code": "TC",
            "latitude": 10.0,
            "longitude": 20.0,
            "timezone": "Etc/UTC",
        }
        data = location_picker.build_config(info, 10.0, 20.0, "city-boundary")
        data["presets"]["tokyo"] = {
            "label": "🇯🇵 Tokyo",
            "menu_label": "🇯🇵 Tokyo",
            "address": "Central Tokyo, Japan",
            "lat": 35.68,
            "lon": 139.76,
            "accuracy_m": 25,
            "datum": "wgs84",
        }
        data["presets"]["ip_city"]["menu_label"] = "🌐 Test City"
        data["presets"]["ip_city"]["address"] = "Test City"
        return data

    def test_rejects_invalid_credentials(self):
        with self.assertRaises(telegram_bot.BotError):
            telegram_bot.validate_token("bad-token")
        with self.assertRaises(telegram_bot.BotError):
            telegram_bot.validate_chat_id("not-a-chat")
        with self.assertRaises(telegram_bot.BotError):
            telegram_bot.validate_chat_id("-1001234567890")

    def test_credential_validation_requires_a_private_chat(self):
        api = mock.Mock()
        api.call.side_effect = [
            {"id": 123456789, "is_bot": True, "username": "test_bot"},
            {"id": int(CHAT_ID), "type": "group"},
        ]
        with mock.patch.object(telegram_bot, "Telegram", return_value=api):
            with self.assertRaisesRegex(telegram_bot.BotError, "chat ID"):
                telegram_bot.validate_credentials(TOKEN, CHAT_ID)

    def test_credential_validation_refuses_to_take_over_a_webhook_bot(self):
        api = mock.Mock()
        api.call.side_effect = [
            {"id": 123456789, "is_bot": True, "username": "existing_bot"},
            {"id": int(CHAT_ID), "type": "private"},
            {"url": "https://example.com/telegram-hook"},
        ]
        with mock.patch.object(telegram_bot, "Telegram", return_value=api):
            with self.assertRaisesRegex(telegram_bot.BotError, "webhook"):
                telegram_bot.validate_credentials(TOKEN, CHAT_ID)

    def test_api_root_requires_https_except_for_loopback_tests(self):
        with mock.patch.dict(
            "os.environ", {"HLE_TELEGRAM_API_ROOT": "http://example.com"}
        ):
            with self.assertRaises(telegram_bot.BotError):
                telegram_bot.telegram_api_root()
        with mock.patch.dict(
            "os.environ", {"HLE_TELEGRAM_API_ROOT": "http://127.0.0.1:19090"}
        ):
            self.assertEqual(
                telegram_bot.telegram_api_root(), "http://127.0.0.1:19090"
            )

    def test_bot_writes_a_private_runtime_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            health = Path(temporary) / "runtime" / "health"
            with mock.patch.object(telegram_bot, "HEALTH_FILE", health):
                bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
                bot._write_health()
            self.assertGreater(float(health.read_text(encoding="ascii")), 0)
            if os.name != "nt":
                self.assertEqual(health.stat().st_mode & 0o777, 0o600)

    def test_run_reports_ready_only_after_a_successful_long_poll(self):
        class PollSequence:
            def __init__(self, first_result):
                self.first_result = first_result
                self.polls = 0
                self.methods = []

            def call(self, method, payload=None, timeout=35):
                self.methods.append(method)
                if method in {
                    "deleteWebhook", "setMyCommands", "setChatMenuButton",
                }:
                    return True
                if method != "getUpdates":
                    raise AssertionError(method)
                self.polls += 1
                if self.polls == 1:
                    if isinstance(self.first_result, BaseException):
                        raise self.first_result
                    return self.first_result
                raise SystemExit("stop test loop")

        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        bot.api = PollSequence(SystemExit("poll never connected"))
        with mock.patch.object(bot, "_write_health") as heartbeat:
            with self.assertRaises(SystemExit):
                bot.run()
            heartbeat.assert_not_called()

        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        bot.api = PollSequence([])
        with mock.patch.object(bot, "_write_health") as heartbeat:
            with self.assertRaises(SystemExit):
                bot.run()
            heartbeat.assert_called_once_with()
        self.assertEqual(
            bot.api.methods[:3],
            ["deleteWebhook", "setMyCommands", "setChatMenuButton"],
        )

    def test_setup_ui_registers_the_input_side_command_menu(self):
        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        bot.api = FakeTelegram()
        bot.setup_ui()

        commands = bot.api.calls[0]
        menu = bot.api.calls[1]
        self.assertEqual(commands[0], "setMyCommands")
        self.assertEqual(
            [item["command"] for item in json.loads(commands[1]["commands"])],
            ["menu", "status", "profile", "node"],
        )
        self.assertEqual(menu[0], "setChatMenuButton")
        self.assertEqual(menu[1]["chat_id"], CHAT_ID)
        self.assertEqual(json.loads(menu[1]["menu_button"]), {"type": "commands"})

    def test_menu_uses_chinese_builtin_labels_and_semantic_button_colors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            location = root / "location.json"
            modifier = root / "modifier.state"
            preset_manager.atomic_write(location, self.make_config())
            modifier.write_text("active\n", encoding="ascii")

            with (
                mock.patch.object(telegram_bot, "LOCATION_FILE", location),
                mock.patch.object(telegram_bot, "MODIFIER_FILE", modifier),
            ):
                bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
                bot.api = FakeTelegram()
                bot.show_menu()
                menu_text = bot.api.calls[-1][1]["text"]
                markup = json.loads(bot.api.calls[-1][1]["reply_markup"])
                buttons = {
                    button["callback_data"]: button
                    for row in markup["inline_keyboard"]
                    for button in row
                }

                self.assertEqual(buttons["loc:set:ip_city"]["text"], "✓ 🌐 出口城市")
                self.assertEqual(buttons["loc:set:ip_city"]["style"], "success")
                self.assertEqual(buttons["loc:set:tokyo"]["text"], "🇯🇵 东京")
                self.assertEqual(buttons["loc:set:tokyo"]["style"], "primary")
                self.assertEqual(buttons["loc:restore"]["style"], "primary")
                self.assertEqual(buttons["loc:add"]["style"], "success")
                self.assertEqual(buttons["loc:delete-menu"]["style"], "danger")
                self.assertEqual(buttons["handoff:profile"]["style"], "primary")
                self.assertEqual(buttons["handoff:node"]["style"], "primary")
                self.assertIn("iPhone 可能继续使用定位缓存", menu_text)
                self.assertIn("隐私与安全性 → 定位服务", menu_text)
                self.assertIn("必要时重启手机", menu_text)
                self.assertNotIn("选择城市立即生效", menu_text)

                modifier.write_text("paused\n", encoding="ascii")
                bot.show_menu()
                paused_markup = json.loads(bot.api.calls[-1][1]["reply_markup"])
                paused_buttons = {
                    button["callback_data"]: button
                    for row in paused_markup["inline_keyboard"]
                    for button in row
                }
                self.assertEqual(paused_buttons["loc:restore"]["style"], "success")
                self.assertEqual(paused_buttons["loc:set:ip_city"]["style"], "primary")

    def test_authorized_chat_can_receive_node_and_profile_handoffs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node_uri = root / "node-uri.txt"
            profile = root / "profile.mobileconfig"
            node_uri.write_text("vless://test-node\n", encoding="utf-8")
            profile_bytes = plistlib.dumps({
                "PayloadType": "Configuration",
                "PayloadContent": [{
                    "PayloadType": "com.apple.security.root",
                    "PayloadContent": b"test-ca",
                }],
            })
            profile.write_bytes(profile_bytes)

            with (
                mock.patch.object(telegram_bot, "NODE_URI_FILE", node_uri),
                mock.patch.object(telegram_bot, "PROFILE_FILE", profile),
            ):
                bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
                bot.api = FakeTelegram()
                bot.handle_callback({"id": "1", "data": "handoff:node"})
                bot.handle_callback({"id": "2", "data": "handoff:profile"})

            node_call = next(
                call for call in bot.api.calls
                if call[0] == "sendMessage" and "vless://" in call[1].get("text", "")
            )
            message_calls = [
                call for call in bot.api.calls if call[0] == "sendMessage"
            ]
            document_call = next(
                call for call in bot.api.calls if call[0] == "sendDocument"
            )
            self.assertEqual(
                message_calls[0][1]["text"],
                telegram_bot.NODE_CREDENTIAL_NOTICE,
            )
            self.assertEqual(message_calls[1][1]["text"], "vless://test-node")
            self.assertNotIn("protect_content", node_call[1])
            self.assertEqual(document_call[1]["content"], profile_bytes)
            self.assertEqual(
                document_call[1]["filename"],
                "Home-Location-Endpoint-CA.mobileconfig",
            )

    def test_install_handoff_sends_four_separate_copyable_messages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "token"
            chat_file = root / "chat_id"
            node_file = root / "node-uri.txt"
            profile_file = root / "Home-Location-Endpoint-CA.mobileconfig"
            token_file.write_text(TOKEN + "\n", encoding="ascii")
            chat_file.write_text(CHAT_ID + "\n", encoding="ascii")
            node_file.write_text("vless://copy-this-node\n", encoding="utf-8")
            profile_file.write_bytes(b"placeholder")
            url = (
                "http://203.0.113.7:18080/"
                "fixed-download-token/Home-Location-Endpoint-CA.mobileconfig"
            )
            api = FakeTelegram()
            with (
                mock.patch.object(telegram_bot, "TOKEN_FILE", token_file),
                mock.patch.object(telegram_bot, "CHAT_FILE", chat_file),
                mock.patch.object(telegram_bot, "NODE_URI_FILE", node_file),
                mock.patch.object(telegram_bot, "PROFILE_FILE", profile_file),
                mock.patch.object(telegram_bot, "Telegram", return_value=api),
            ):
                telegram_bot.send_install_handoff(url, 100)

            messages = [
                call[1] for call in api.calls if call[0] == "sendMessage"
            ]
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages[0]["text"], telegram_bot.NODE_CREDENTIAL_NOTICE)
            self.assertEqual(messages[1]["text"], "vless://copy-this-node")
            self.assertNotIn("vless://", messages[0]["text"])
            self.assertIn("iPhone Safari", messages[2]["text"])
            self.assertIn("有效 100 分钟", messages[2]["text"])
            self.assertIn("首次成功下载后立即失效", messages[2]["text"])
            self.assertEqual(messages[3]["text"], url)
            self.assertTrue(all(
                message["disable_web_page_preview"] == "true"
                for message in messages
            ))

    def test_install_handoff_rejects_a_malformed_download_url(self):
        with self.assertRaises(telegram_bot.BotError):
            telegram_bot.validate_profile_download_url(
                "https://example.com/not-a-profile"
            )

    def test_send_document_uses_installable_multipart_upload(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read(_limit):
                return b'{"ok":true,"result":{"message_id":1}}'

        api = telegram_bot.Telegram(TOKEN)
        with mock.patch.object(
            telegram_bot.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            api.send_document(
                CHAT_ID,
                "Home-Location-Endpoint-CA.mobileconfig",
                b"profile-content",
                "CA profile",
            )
        request = urlopen.call_args.args[0]
        content_type = request.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertNotIn(b'name="protect_content"', request.data)
        self.assertIn(b"profile-content", request.data)
        self.assertIn(b"Home-Location-Endpoint-CA.mobileconfig", request.data)

    def test_handoff_rejects_unexpected_or_malformed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node_uri = root / "node-uri.txt"
            profile = root / "profile.mobileconfig"
            node_uri.write_text("https://example.com\n", encoding="utf-8")
            profile.write_text("not a plist", encoding="ascii")
            with (
                mock.patch.object(telegram_bot, "NODE_URI_FILE", node_uri),
                mock.patch.object(telegram_bot, "PROFILE_FILE", profile),
            ):
                with self.assertRaises(telegram_bot.BotError):
                    telegram_bot.load_node_uri()
                with self.assertRaises(telegram_bot.BotError):
                    telegram_bot.load_profile()

    def test_authorized_callbacks_switch_and_restore_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            location = root / "location.json"
            modifier = root / "modifier.state"
            backup = root / "backups"
            offset = root / "offset"
            lock = root / "lock"
            preset_manager.atomic_write(location, self.make_config())
            modifier.write_text("active\n", encoding="ascii")

            patches = (
                mock.patch.object(telegram_bot, "LOCATION_FILE", location),
                mock.patch.object(telegram_bot, "MODIFIER_FILE", modifier),
                mock.patch.object(telegram_bot, "BACKUP_DIR", backup),
                mock.patch.object(telegram_bot, "OFFSET_FILE", offset),
                mock.patch.object(telegram_bot, "LOCK_FILE", lock),
            )
            for patch in patches:
                patch.start()
            self.addCleanup(lambda: [patch.stop() for patch in reversed(patches)])

            bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
            bot.api = FakeTelegram()
            bot.handle_callback({"id": "1", "data": "loc:set:tokyo"})
            self.assertEqual(preset_manager.load(location)["active"], "tokyo")
            self.assertEqual(modifier.read_text(encoding="ascii"), "active\n")

            bot.handle_callback({"id": "2", "data": "loc:restore"})
            self.assertEqual(modifier.read_text(encoding="ascii"), "paused\n")
            self.assertTrue(any(call[0] == "sendMessage" for call in bot.api.calls))

    def test_unauthorized_chat_is_ignored(self):
        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        bot.api = FakeTelegram()
        bot.process_update({
            "update_id": 1,
            "message": {
                "chat": {"id": 111111, "type": "private"},
                "from": {"id": 111111},
                "text": "/menu",
            },
        })
        self.assertEqual(bot.api.calls, [])

    def test_forged_sender_in_authorized_chat_is_ignored(self):
        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        bot.api = FakeTelegram()
        bot.process_update({
            "update_id": 1,
            "message": {
                "chat": {"id": int(CHAT_ID), "type": "private"},
                "from": {"id": 111111},
                "text": "/menu",
            },
        })
        self.assertEqual(bot.api.calls, [])

    def test_authorized_private_sender_is_processed(self):
        bot = telegram_bot.LocationBot(TOKEN, CHAT_ID)
        with mock.patch.object(bot, "handle_text") as handle_text:
            bot.process_update({
                "update_id": 1,
                "message": {
                    "chat": {"id": int(CHAT_ID), "type": "private"},
                    "from": {"id": int(CHAT_ID)},
                    "text": "/menu",
                },
            })
        handle_text.assert_called_once_with("/menu")


if __name__ == "__main__":
    unittest.main()
