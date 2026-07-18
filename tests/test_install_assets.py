import json
import re
import unittest
from pathlib import Path

from home_location_endpoint import render


ROOT = Path(__file__).resolve().parents[1]


class InstallAssetTests(unittest.TestCase):
    def test_reality_sni_pool_is_unique_and_valid(self):
        entries = [
            line.strip()
            for line in (ROOT / "configs" / "reality-sni.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(entries), 32)
        self.assertEqual(len(entries), len(set(entries)))
        for entry in entries:
            self.assertEqual(render.validate_host(entry, allow_ip=False), entry)

    def test_modifier_only_xray_fragment_matches_full_mode(self):
        fragment = json.loads(
            (ROOT / "configs" / "xray-location-routing.example.json").read_text(
                encoding="utf-8"
            )
        )
        full = render.build_xray_config(
            port=443,
            client_uuid="12345678-1234-4234-8234-123456789abc",
            reality_sni="www.microsoft.com",
            reality_target="www.microsoft.com:443",
            private_key="A" * 43,
            short_id="0123456789abcdef",
        )
        self.assertEqual(fragment["outbounds"], full["outbounds"][1:])
        expected_rules = []
        for rule in full["routing"]["rules"]:
            expected_rule = dict(rule)
            expected_rule.pop("inboundTag")
            expected_rules.append(expected_rule)
        self.assertEqual(fragment["routing"]["rules"], expected_rules)

    def test_nontransactional_side_effects_follow_final_verification(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        verify = installer.index("/usr/local/sbin/hle verify")
        commit = installer.index("TRANSACTION_COMMITTED=1", verify)
        apply_sysctl = installer.index("apply_baseline", commit)
        firewall = installer.index("open_active_firewall", commit)
        self.assertLess(verify, commit)
        self.assertLess(commit, apply_sysctl)
        self.assertLess(commit, firewall)

    def test_bootstrap_version_is_validated_before_package_changes(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        bootstrap = installer.index("bootstrap_if_needed()")
        validation = installer.index("HLE_VERSION contains unsupported", bootstrap)
        apt_update = installer.index("apt-get -o Acquire::Retries=3", bootstrap)
        self.assertLess(validation, apt_update)

    def test_apt_calls_tolerate_a_held_lock(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        apt_calls = re.findall(r"apt-get -o Acquire::Retries=3[^\n]*", installer)
        self.assertTrue(apt_calls)
        for call in apt_calls:
            self.assertIn("DPkg::Lock::Timeout=", call)
        packages = installer.index("install_packages()")
        wait = installer.index("wait_for_apt_lock", packages)
        first_apt = installer.index("apt-get", packages)
        self.assertLess(wait, first_apt)

    def test_service_and_log_limits_are_present(self):
        service = (ROOT / "systemd" / "home-location-endpoint.service").read_text(
            encoding="utf-8"
        )
        logrotate = (
            ROOT / "configs" / "home-location-endpoint.logrotate"
        ).read_text(encoding="utf-8")
        self.assertIn("MemoryMax=256M", service)
        self.assertIn("TasksMax=64", service)
        self.assertIn("maxsize 16M", logrotate)


if __name__ == "__main__":
    unittest.main()
