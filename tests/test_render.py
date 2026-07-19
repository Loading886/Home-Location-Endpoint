import plistlib
import base64
import unittest

from home_location_endpoint import render


UUID = "12345678-1234-4234-8234-123456789abc"
PRIVATE_KEY = "A" * 43
PUBLIC_KEY = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI"
SHORT_ID = "0123456789abcdef"
SS_PASSWORD = base64.b64encode(b"s" * 32).decode("ascii")


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

        quic_rule, rule = config["routing"]["rules"]
        self.assertEqual(quic_rule["network"], "udp")
        self.assertEqual(quic_rule["outboundTag"], "block-location-quic")
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
        self.assertEqual(config["outbounds"][2]["protocol"], "blackhole")

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
        encoded = render.build_ca_profile(b"fake-der")
        profile = plistlib.loads(encoded)
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertEqual(len(profile["PayloadContent"]), 1)
        payload = profile["PayloadContent"][0]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        self.assertEqual(payload["PayloadContent"], b"fake-der")
        self.assertEqual(encoded, render.build_ca_profile(b"fake-der"))
        self.assertNotEqual(encoded, render.build_ca_profile(b"different-der"))

    def test_rejects_odd_short_id(self):
        with self.assertRaises(ValueError):
            render.validate_short_id("abc")

    def test_rejects_noncanonical_x25519_key(self):
        with self.assertRaises(ValueError):
            render.validate_key("B" * 43, "REALITY public key")

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

    def test_fallback_limits_are_validated_and_rendered(self):
        limit = {
            "afterBytes": 8 * 1024 * 1024,
            "bytesPerSec": 768 * 1024,
            "burstBytesPerSec": 3 * 1024 * 1024,
        }
        config = render.build_xray_config(
            port=443,
            client_uuid=UUID,
            reality_sni="www.microsoft.com",
            reality_target="www.microsoft.com:443",
            private_key=PRIVATE_KEY,
            short_id=SHORT_ID,
            fallback_upload=limit,
            fallback_download=limit,
        )
        settings = config["inbounds"][0]["streamSettings"]["realitySettings"]
        self.assertEqual(settings["limitFallbackUpload"], limit)
        self.assertEqual(settings["limitFallbackDownload"], limit)
        with self.assertRaises(ValueError):
            render.validate_fallback_limit({
                "afterBytes": 1,
                "bytesPerSec": 10,
                "burstBytesPerSec": 1,
            })

    def test_dual_stack_listener_is_explicit(self):
        config = render.build_xray_config(
            port=443,
            client_uuid=UUID,
            reality_sni="www.microsoft.com",
            reality_target="www.microsoft.com:443",
            private_key=PRIVATE_KEY,
            short_id=SHORT_ID,
            listen_host="::",
        )
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen"], "::")
        self.assertEqual(inbound["streamSettings"]["sockopt"], {"v6only": False})

    def test_ss2022_config_reuses_scoped_location_routing(self):
        config = render.build_xray_config(
            port=443,
            protocol="ss2022",
            ss_password=SS_PASSWORD,
        )
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["tag"], "ss2022-in")
        self.assertEqual(inbound["protocol"], "shadowsocks")
        self.assertEqual(inbound["settings"], {
            "network": "tcp,udp",
            "method": render.SS2022_METHOD,
            "password": SS_PASSWORD,
        })
        self.assertTrue(inbound["sniffing"]["routeOnly"])
        for rule in config["routing"]["rules"]:
            self.assertEqual(rule["inboundTag"], ["ss2022-in"])

    def test_ss2022_uri_and_password_validation(self):
        uri = render.build_ss2022_uri(
            server="203.0.113.9", port=443, password=SS_PASSWORD
        )
        self.assertTrue(uri.startswith("ss://"))
        encoded = uri.split("//", 1)[1].split("@", 1)[0]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        self.assertEqual(
            decoded.decode("ascii"), "%s:%s" % (render.SS2022_METHOD, SS_PASSWORD)
        )
        with self.assertRaises(ValueError):
            render.validate_ss2022_password("not-a-32-byte-key")


if __name__ == "__main__":
    unittest.main()
