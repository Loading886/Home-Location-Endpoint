import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from home_location_endpoint import render


ROOT = Path(__file__).resolve().parents[1]


class InstallAssetTests(unittest.TestCase):
    def test_package_and_project_versions_match(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        package = (
            ROOT / "src" / "home_location_endpoint" / "__init__.py"
        ).read_text(encoding="utf-8")
        project_version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
        package_version = re.search(r'^__version__ = "([^"]+)"$', package, re.MULTILINE)
        self.assertIsNotNone(project_version)
        self.assertIsNotNone(package_version)
        self.assertEqual(project_version.group(1), package_version.group(1))

    def test_installer_uses_only_the_fixed_reality_sni(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('REALITY_SNI="www.usc.edu"', installer)
        self.assertIn('REALITY_TARGET="www.usc.edu:443"', installer)
        self.assertNotIn("reality-sni.txt", installer)
        self.assertNotIn("shuf", installer)
        self.assertNotIn("--reality-sni)", installer)
        self.assertNotIn("--reality-target)", installer)

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

    def test_missing_service_state_probes_are_silent(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        for service in ("home-location-endpoint.service", "xray.service"):
            for state in ("is-active", "is-enabled"):
                self.assertRegex(
                    installer,
                    rf"systemctl {state} --quiet {re.escape(service)} "
                    rf"\\\n\s+>/dev/null 2>&1",
                )

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

    def test_apt_never_auto_restarts_unrelated_services(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        install_calls = re.findall(
            r"DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=([a-z])", installer
        )
        self.assertGreaterEqual(len(install_calls), 3)
        self.assertEqual(set(install_calls), {"l"})
        self.assertNotIn("NEEDRESTART_MODE=a", installer)

    def test_interactive_install_and_result_are_bilingual(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        for message in (
            "下载中……请稍等",
            "选择安装模式：",
            "新手模式：安装完整代理节点和定位修改器",
            "进阶模式：增加 Telegram 定位菜单",
            "高手模式：仅安装定位修改器",
            "完整模式安装完成。",
            "仅定位修改器模式安装完成。",
            "Next / 下一步:",
            "安装器未修改 SSH。",
            "sudo hle profile serve",
        ):
            self.assertIn(message, installer)

    def test_profile_handoff_runs_only_after_the_install_commits(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        commit = installer.index("TRANSACTION_COMMITTED=1")
        result = installer.index("show_result", commit)
        handoff = installer.index("auto_serve_profile", result)
        self.assertLess(commit, result)
        self.assertLess(result, handoff)
        self.assertIn("if ! interactive_output", installer)
        self.assertIn("if serve_profile_download", installer)
        self.assertIn("130|143", installer)
        self.assertIn("CA 临时下载已由用户关闭", installer)

    def test_installer_preserves_a_valid_modifier_state(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -f "${STATE_DIR}/modifier.state" ]]', installer)
        self.assertIn("active|paused", installer)
        self.assertIn('chmod 0644 "${STATE_DIR}/modifier.state"', installer)

    def test_qrencode_is_installed_for_profile_handoff(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("apt-cache show qrencode", installer)
        self.assertIn("optional qrencode installation failed", installer)
        self.assertIn("profile download URLs will still work", installer)

    def test_installed_cli_starts_without_the_source_package(self):
        source = ROOT / "src" / "home_location_endpoint" / "cli.py"
        with tempfile.TemporaryDirectory() as temporary:
            standalone = Path(temporary) / "hle"
            shutil.copy2(source, standalone)
            result = subprocess.run(
                [sys.executable, "-I", str(standalone), "--help"],
                cwd=temporary,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: hle", result.stdout)

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

    def test_advanced_bot_is_low_privilege_and_does_not_receive_node_secrets(self):
        service = (
            ROOT / "systemd" / "home-location-telegram-bot.service"
        ).read_text(encoding="utf-8")
        self.assertIn("User=home-location-bot", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("InaccessiblePaths=/etc/home-location-endpoint/install.env", service)
        self.assertIn("/etc/home-location-endpoint/node-uri.txt", service)
        self.assertIn("RuntimeDirectory=home-location-endpoint-bot", service)
        self.assertIn("HLE_TELEGRAM_HEALTH_FILE=", service)
        self.assertNotIn("0.0.0.0", service)

    def test_advanced_catalog_contains_the_documented_cities(self):
        catalog = json.loads(
            (ROOT / "configs" / "advanced-location-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(catalog["presets"]), {
            "los_angeles", "tokyo", "hong_kong", "singapore",
            "kuala_lumpur", "paris", "frankfurt", "reykjavik",
            "kunlun_station",
        })


if __name__ == "__main__":
    unittest.main()
