import plistlib
import unittest

from home_location_endpoint import render


UUID = "12345678-1234-4234-8234-123456789abc"
PRIVATE_KEY = "A" * 43
PUBLIC_KEY = "B" * 43
SHORT_ID = "0123456789abcdef"


class RenderTests(unittest.TestCase):
    def build_config(self):
        return render.build_xray_config(
            port=443,
            client_uuid=UUID,
            reality_sni="www.microsoft.com",
            reality_target="www.microsoft.com:443",
            private_key=PRIVATE_KEY,
            short_id=SHORT_ID,
        )

    def test_xray_config_scopes_location_interception(self):
        config = self.build_config()
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["protocol"], "vless")
        self.assertEqual(inbound["streamSettings"]["security"], "reality")
        self.assertTrue(inbound["sniffing"]["routeOnly"])
        self.assertEqual(config["outbounds"][0]["tag"], "direct")

        rule = config["routing"]["rules"][0]
        self.assertEqual(rule["port"], "443")
        self.assertEqual(rule["network"], "tcp")
        self.assertEqual(rule["domain"], render.LOCATION_DOMAINS)
        self.assertEqual(rule["outboundTag"], "location-interceptor")

        local = config["outbounds"][1]["settings"]
        self.assertEqual(local["redirect"], "127.0.0.1:10451")
        self.assertEqual(local["finalRules"], [{
            "action": "allow",
            "network": "tcp",
            "ip": ["127.0.0.1/32"],
            "port": "10451",
        }])

    def test_uri_contains_reality_vision_parameters(self):
        uri = render.build_vless_uri(
            server="203.0.113.9",
            port=443,
            client_uuid=UUID,
            reality_sni="www.microsoft.com",
            public_key=PUBLIC_KEY,
            short_id=SHORT_ID,
        )
        self.assertTrue(uri.startswith("vless://%s@203.0.113.9:443?" % UUID))
        self.assertIn("flow=xtls-rprx-vision", uri)
        self.assertIn("security=reality", uri)
        self.assertIn("pbk=%s" % PUBLIC_KEY, uri)
        self.assertIn("sid=%s" % SHORT_ID, uri)
        self.assertIn("packetEncoding=xudp", uri)

    def test_ca_profile_contains_only_one_certificate_payload(self):
        profile = plistlib.loads(render.build_ca_profile(b"fake-der"))
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertEqual(len(profile["PayloadContent"]), 1)
        payload = profile["PayloadContent"][0]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        self.assertEqual(payload["PayloadContent"], b"fake-der")

    def test_rejects_odd_short_id(self):
        with self.assertRaises(ValueError):
            render.validate_short_id("abc")

    def test_rejects_invalid_reality_sni(self):
        with self.assertRaises(ValueError):
            render.build_xray_config(
                port=443,
                client_uuid=UUID,
                reality_sni="bad host",
                reality_target="www.microsoft.com:443",
                private_key=PRIVATE_KEY,
                short_id=SHORT_ID,
            )


if __name__ == "__main__":
    unittest.main()
