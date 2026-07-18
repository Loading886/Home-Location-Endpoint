import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from home_location_endpoint import cli, render


class CliTests(unittest.TestCase):
    def test_uninstall_requires_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(SystemExit, "must run as root"):
                cli.command_uninstall(mock.Mock(yes=True))

    def test_uninstall_is_a_registered_command(self):
        with mock.patch("sys.argv", ["hle", "uninstall", "--yes"]):
            args = cli.parse_args()
        self.assertIs(args.func, cli.command_uninstall)
        self.assertTrue(args.yes)

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
