import unittest

from home_location_endpoint import interceptor


class InterceptorTests(unittest.TestCase):
    def test_host_scope_is_narrow(self):
        allowed = [
            "gs-loc.apple.com",
            "gs-loc-cn.apple.com",
            "gspe85-ssl.ls.apple.com",
            "gspe85-12-ssl.ls.apple.com",
            "gspe85-cn-ssl.ls.apple.com",
            "gspe85-9-cn-ssl.ls.apple.com",
        ]
        for host in allowed:
            self.assertTrue(interceptor.is_allowed_host(host), host)
        for host in ("apple.com", "maps.apple.com", "evil-gspe85-ssl.ls.apple.com"):
            self.assertFalse(interceptor.is_allowed_host(host), host)

    def test_cn_wifi_tile_uses_global_origin_only_for_tile_path(self):
        host = "gspe85-cn-ssl.ls.apple.com"
        self.assertEqual(
            interceptor.select_origin_host(host, "GET", "/wifi_request_tile?x=1"),
            interceptor.WIFI_TILE_GLOBAL_HOST,
        )
        self.assertEqual(
            interceptor.select_origin_host(host, "GET", "/other"), host
        )

    def test_upstream_request_strips_hop_by_hop_headers(self):
        request = interceptor.build_upstream_request(
            "gs-loc.apple.com",
            "POST /clls/wloc HTTP/1.1",
            [
                "Host: old.invalid",
                "Connection: keep-alive",
                "Transfer-Encoding: chunked",
                "X-Apple-Test: retained",
            ],
            b"abc",
        ).decode("latin1")
        self.assertIn("Host: gs-loc.apple.com", request)
        self.assertIn("X-Apple-Test: retained", request)
        self.assertIn("Content-Length: 3", request)
        self.assertNotIn("Transfer-Encoding", request)
        self.assertNotIn("old.invalid", request)


if __name__ == "__main__":
    unittest.main()
