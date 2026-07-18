import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

from home_location_endpoint import cli, render


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
