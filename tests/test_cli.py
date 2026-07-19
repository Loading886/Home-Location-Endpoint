import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

from home_location_endpoint import cli, render, telegram_bot


class CliTests(unittest.TestCase):
    def test_bot_heartbeat_must_exist_and_be_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            health = Path(temporary) / "health"
            with mock.patch.object(cli, "BOT_HEALTH_FILE", health):
                self.assertFalse(cli.bot_health_is_fresh())
                health.write_text("%.6f\n" % time.time(), encoding="ascii")
                self.assertTrue(cli.bot_health_is_fresh())
                health.write_text("%.6f\n" % (time.time() - 181), encoding="ascii")
                self.assertFalse(cli.bot_health_is_fresh())

    def test_pause_and_resume_are_registered_commands(self):
        with mock.patch("sys.argv", ["hle", "pause"]):
            pause = cli.parse_args()
        with mock.patch("sys.argv", ["hle", "resume"]):
            resume = cli.parse_args()
        self.assertIs(pause.func, cli.command_pause)
        self.assertIs(resume.func, cli.command_resume)

    def test_pause_requires_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=1000, create=True):
            with self.assertRaisesRegex(SystemExit, "must run as root"):
                cli.command_pause(None)

    def test_pause_persists_without_restarting_services(self):
        output = io.StringIO()
        with (
            mock.patch.object(cli.os, "geteuid", return_value=0, create=True),
            mock.patch.object(cli, "operation_lock", return_value=nullcontext()),
            mock.patch.object(cli, "modifier_state", return_value="active"),
            mock.patch.object(cli, "_write_modifier_state") as write_state,
            redirect_stdout(output),
        ):
            cli.command_pause(None)
        write_state.assert_called_once_with("paused")
        self.assertIn("代理流量保持正常", output.getvalue())

    def test_modifier_state_defaults_active_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(cli, "STATE", state):
                self.assertEqual(cli.modifier_state(), "active")
                (state / cli.MODIFIER_STATE_NAME).write_text(
                    "paused\n", encoding="ascii"
                )
                self.assertEqual(cli.modifier_state(), "paused")
                (state / cli.MODIFIER_STATE_NAME).write_text(
                    "broken\n", encoding="ascii"
                )
                with self.assertRaisesRegex(ValueError, "invalid modifier state"):
                    cli.modifier_state()

    def test_uninstall_requires_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=1000, create=True):
            with self.assertRaisesRegex(SystemExit, "must run as root"):
                cli.command_uninstall(mock.Mock(yes=True))

    def test_uninstall_is_a_registered_command(self):
        with mock.patch("sys.argv", ["hle", "uninstall", "--yes"]):
            args = cli.parse_args()
        self.assertIs(args.func, cli.command_uninstall)
        self.assertTrue(args.yes)

    def _run_modifier_uninstall(self, inventory, *, remove_ok=True):
        def systemctl(*arguments):
            return 1 if arguments[:2] == ("is-active", "--quiet") else 0

        patches = [
            mock.patch.object(cli.os, "geteuid", return_value=0, create=True),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(cli, "_valid_installer_marker", return_value=True),
            mock.patch.object(cli, "install_mode", return_value="modifier-only"),
            mock.patch.object(cli, "_install_inventory", return_value=inventory),
            mock.patch.object(cli, "operation_lock", return_value=nullcontext()),
            mock.patch.object(cli, "_systemctl", side_effect=systemctl),
            mock.patch.object(cli, "_remove_path", return_value=remove_ok),
            mock.patch.object(cli, "_delete_user", return_value=True),
            mock.patch.object(cli, "_delete_group", return_value=True),
        ]
        started = [patch.start() for patch in patches]
        self.addCleanup(lambda: [patch.stop() for patch in reversed(patches)])
        with redirect_stdout(io.StringIO()):
            cli.command_uninstall(mock.Mock(yes=True))
        return started[-2], started[-1]

    def test_uninstall_preserves_accounts_without_creation_inventory(self):
        delete_user, delete_group = self._run_modifier_uninstall({})
        delete_user.assert_not_called()
        delete_group.assert_not_called()

    def test_uninstall_deletes_only_accounts_recorded_as_created(self):
        delete_user, delete_group = self._run_modifier_uninstall({
            "HLE_CREATED_HOME_USER": "1",
            "HLE_CREATED_HOME_GROUP": "1",
        })
        delete_user.assert_called_once_with("home-location")
        delete_group.assert_called_once_with("home-location")

    def test_uninstall_reports_partial_removal(self):
        with self.assertRaisesRegex(SystemExit, "1"):
            self._run_modifier_uninstall({}, remove_ok=False)

    def _run_advanced_uninstall(self, inventory):
        def systemctl(*arguments):
            return 1 if arguments[:2] == ("is-active", "--quiet") else 0

        patches = [
            mock.patch.object(cli.os, "geteuid", return_value=0, create=True),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/systemctl"),
            mock.patch.object(cli, "_valid_installer_marker", return_value=True),
            mock.patch.object(cli, "install_mode", return_value="advanced"),
            mock.patch.object(cli, "_install_inventory", return_value=inventory),
            mock.patch.object(cli, "operation_lock", return_value=nullcontext()),
            mock.patch.object(cli, "_systemctl", side_effect=systemctl),
            mock.patch.object(cli, "_remove_path", return_value=True),
            mock.patch.object(cli, "_remove_group_membership", return_value=True),
            mock.patch.object(cli, "_delete_user", return_value=True),
            mock.patch.object(cli, "_delete_group", return_value=True),
        ]
        started = [patch.start() for patch in patches]
        self.addCleanup(lambda: [patch.stop() for patch in reversed(patches)])
        with redirect_stdout(io.StringIO()):
            cli.command_uninstall(mock.Mock(yes=True))
        return started[-3], started[-2], started[-1]

    def test_uninstall_removes_installer_added_membership_from_preserved_bot(self):
        remove_membership, delete_user, _delete_group = (
            self._run_advanced_uninstall({
                "HLE_ADDED_BOT_HOME_MEMBERSHIP": "1",
            })
        )
        remove_membership.assert_called_once_with(
            "home-location-bot", "home-location"
        )
        delete_user.assert_not_called()

    def test_uninstall_does_not_separately_remove_membership_with_bot_user(self):
        remove_membership, delete_user, _delete_group = (
            self._run_advanced_uninstall({
                "HLE_ADDED_BOT_HOME_MEMBERSHIP": "1",
                "HLE_CREATED_BOT_USER": "1",
            })
        )
        remove_membership.assert_not_called()
        delete_user.assert_called_once_with("home-location-bot")

    def test_install_mode_rejects_corrupt_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            etc = Path(temporary)
            (etc / "mode").write_text("unexpected\n", encoding="utf-8")
            with mock.patch.object(cli, "ETC", etc):
                with self.assertRaisesRegex(SystemExit, "invalid installation mode"):
                    cli.install_mode()

    def test_profile_command_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(cli, "ETC", Path(temporary)):
                with self.assertRaisesRegex(SystemExit, "CA profile is missing"):
                    cli.command_profile(None)

    def test_profile_serve_is_registered_with_100_minute_default(self):
        with mock.patch("sys.argv", ["hle", "profile", "serve"]):
            args = cli.parse_args()
        self.assertIs(args.func, cli.command_profile_serve)
        self.assertEqual(args.port, 18080)
        self.assertEqual(args.timeout_minutes, 100)
        self.assertFalse(args.notify_telegram)

    def test_profile_telegram_notification_requires_advanced_mode(self):
        with mock.patch.object(cli, "install_mode", return_value="full"):
            with self.assertRaisesRegex(SystemExit, "advanced-mode"):
                cli._notify_telegram_install_handoff("http://example.com/file", 100)

    def test_profile_telegram_notification_delegates_to_the_bot(self):
        url = (
            "http://203.0.113.7:18080/"
            "fixed-download-token/Home-Location-Endpoint-CA.mobileconfig"
        )
        with (
            mock.patch.object(cli, "install_mode", return_value="advanced"),
            mock.patch.object(telegram_bot, "send_install_handoff") as notify,
        ):
            cli._notify_telegram_install_handoff(url, 100)
        notify.assert_called_once_with(url, 100)

    def test_profile_serve_requires_a_client_reachable_host(self):
        with mock.patch.object(cli, "_install_inventory", return_value={}):
            with self.assertRaisesRegex(SystemExit, "use --host"):
                cli._profile_download_host(None)

    def test_profile_serve_downloads_once_with_apple_mime(self):
        with tempfile.TemporaryDirectory() as temporary:
            etc = Path(temporary)
            ca_der = b"temporary-download-test-ca"
            profile_bytes = render.build_ca_profile(ca_der)
            (etc / "ca.der").write_bytes(ca_der)
            (etc / cli.PROFILE_NAME).write_bytes(profile_bytes)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            args = mock.Mock(
                host="127.0.0.1",
                bind="127.0.0.1",
                port=port,
                timeout_minutes=1,
                no_qr=True,
                notify_telegram=True,
            )
            output = io.StringIO()
            token = "fixed-download-token"
            expected_url = (
                "http://127.0.0.1:%d/%s/%s"
                % (port, token, cli.PROFILE_NAME)
            )
            with (
                mock.patch.object(cli, "ETC", etc),
                mock.patch.object(cli.secrets, "token_urlsafe", return_value=token),
                mock.patch.object(cli, "_notify_telegram_install_handoff") as notify,
                redirect_stdout(output),
            ):
                worker = threading.Thread(
                    target=cli.command_profile_serve, args=(args,), daemon=True
                )
                worker.start()
                base = "http://127.0.0.1:%d" % port
                deadline = time.monotonic() + 3
                while True:
                    try:
                        urllib.request.urlopen(base + "/wrong", timeout=0.2)
                    except urllib.error.HTTPError as exc:
                        try:
                            self.assertEqual(exc.code, 404)
                        finally:
                            exc.close()
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            self.fail("temporary profile server did not start")
                        time.sleep(0.02)

                with urllib.request.urlopen(
                    base + "/%s/%s" % (token, cli.PROFILE_NAME), timeout=1
                ) as response:
                    self.assertEqual(response.read(), profile_bytes)
                    self.assertEqual(
                        response.headers.get_content_type(),
                        "application/x-apple-aspen-config",
                    )
                    self.assertIn(
                        cli.PROFILE_NAME,
                        response.headers["Content-Disposition"],
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive())
            notify.assert_called_once_with(expected_url, 1)
            self.assertIn("Profile downloaded; server closed.", output.getvalue())

    def test_profile_and_location_integrity_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            etc = Path(temporary)
            ca_der = b"test-ca-der"
            (etc / "ca.der").write_bytes(ca_der)
            (etc / "Home-Location-Endpoint-CA.mobileconfig").write_bytes(
                render.build_ca_profile(ca_der)
            )
            (etc / "location.json").write_text(
                json.dumps({
                    "active": "current",
                    "presets": {
                        "current": {
                            "lat": 34.0,
                            "lon": -118.0,
                            "datum": "wgs84",
                        }
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(cli, "ETC", etc):
                self.assertTrue(cli.profile_matches_ca())
                self.assertTrue(cli.location_is_valid())

    def test_advanced_handoff_must_match_root_managed_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            etc = Path(temporary)
            telegram = etc / "telegram"
            telegram.mkdir()
            node_uri = b"vless://test-node\n"
            profile = b"profile-bytes"
            (etc / "node-uri.txt").write_bytes(node_uri)
            (telegram / "node-uri.txt").write_bytes(node_uri)
            (etc / cli.PROFILE_NAME).write_bytes(profile)
            (telegram / cli.PROFILE_NAME).write_bytes(profile)
            with (
                mock.patch.object(cli, "ETC", etc),
                mock.patch.object(cli, "install_mode", return_value="advanced"),
            ):
                self.assertTrue(cli.advanced_handoff_matches())
                (telegram / "node-uri.txt").write_text(
                    "ss://stale-node\n", encoding="utf-8"
                )
                self.assertFalse(cli.advanced_handoff_matches())


if __name__ == "__main__":
    unittest.main()
