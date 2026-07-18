import json
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
        self.assertEqual(fragment["outbounds"][0], full["outbounds"][1])
        expected_rule = dict(full["routing"]["rules"][0])
        expected_rule.pop("inboundTag")
        self.assertEqual(fragment["routing"]["rules"][0], expected_rule)


if __name__ == "__main__":
    unittest.main()
