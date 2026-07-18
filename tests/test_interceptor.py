import io
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
        self.assertEqual(
            interceptor.normalize_location_host("GS-LOC.APPLE.COM.:443"),
            "gs-loc.apple.com",
        )
        with self.assertRaises(ValueError):
            interceptor.normalize_location_host("gs-loc.apple.com:invalid")

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

    def test_header_parser_rejects_ambiguous_framing(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            interceptor.header_map(["Content-Length: 1", "Content-Length: 1"])
        with self.assertRaisesRegex(ValueError, "both Content-Length"):
            interceptor.header_map([
                "Content-Length: 1",
                "Transfer-Encoding: chunked",
            ])
        with self.assertRaisesRegex(ValueError, "folded"):
            interceptor.header_map([" continuation"])
        with self.assertRaisesRegex(ValueError, "control character"):
            interceptor.header_map(["X-Test: value\x7f"])

    def test_chunked_body_requires_valid_crlf_and_consumes_trailers(self):
        valid = io.BytesIO(b"3\r\nabc\r\n0\r\nX-Test: yes\r\n\r\n")
        self.assertEqual(
            interceptor.read_body(valid, {"transfer-encoding": "chunked"}),
            b"abc",
        )
        broken = io.BytesIO(b"3\r\nabcXX0\r\n\r\n")
        with self.assertRaisesRegex(ValueError, "CRLF"):
            interceptor.read_body(broken, {"transfer-encoding": "chunked"})

    def test_request_scope_uses_exact_wloc_path(self):
        self.assertTrue(
            interceptor.is_wloc_request(
                "gs-loc.apple.com", "POST", "/clls/wloc?x=1"
            )
        )
        self.assertFalse(
            interceptor.is_wloc_request(
                "gs-loc.apple.com", "POST", "/clls/wloc-unrelated"
            )
        )
        with self.assertRaisesRegex(ValueError, "origin-form"):
            interceptor.validate_request_line("GET /bad\x00path HTTP/1.1")

    def test_response_strips_connection_nominated_headers(self):
        response = interceptor.build_response(
            "HTTP/1.1 200 OK",
            [
                "Connection: X-Remove",
                "X-Remove: secret",
                "Keep-Alive: timeout=5",
                "Content-Type: application/octet-stream",
            ],
            {"connection": "X-Remove"},
            b"ok",
        ).decode("latin1")
        self.assertNotIn("X-Remove", response)
        self.assertNotIn("Keep-Alive", response)
        self.assertIn("Content-Length: 2", response)

    def test_upstream_parser_skips_informational_response(self):
        reader = io.BytesIO(
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )
        status, _lines, _headers, body = interceptor.read_final_response(reader)
        self.assertEqual(status, "HTTP/1.1 200 OK")
        self.assertEqual(body, b"ok")


if __name__ == "__main__":
    unittest.main()
