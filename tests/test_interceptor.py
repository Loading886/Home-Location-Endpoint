import gzip
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from home_location_endpoint import interceptor
from home_location_endpoint import gsloc_rewrite as gx
from home_location_endpoint import wifitile_rewrite as wx


class InterceptorTests(unittest.TestCase):
    def setUp(self):
        with interceptor._recent_wifi_lock:
            interceptor._recent_wifi.clear()
        with interceptor._wifi_template_lock:
            interceptor._wifi_template_cache.update(payload=None, seen=None)

    def test_modifier_state_defaults_active_and_accepts_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "modifier.state"
            with mock.patch.object(interceptor, "MODIFIER_STATE", str(state)):
                self.assertTrue(interceptor.modification_is_active())
                state.write_text("paused\n", encoding="ascii")
                self.assertFalse(interceptor.modification_is_active())
                state.write_text("active\n", encoding="ascii")
                self.assertTrue(interceptor.modification_is_active())
                state.write_text("unexpected\n", encoding="ascii")
                with self.assertRaisesRegex(RuntimeError, "invalid modifier state"):
                    interceptor.modification_is_active()

    def test_paused_modifier_preserves_the_requested_wifi_tile_origin(self):
        host = "gspe85-cn-ssl.ls.apple.com"
        self.assertEqual(
            interceptor.effective_origin_host(
                host, "GET", "/wifi_request_tile", False
            ),
            host,
        )
        self.assertEqual(
            interceptor.effective_origin_host(
                host, "GET", "/wifi_request_tile", True
            ),
            interceptor.WIFI_TILE_GLOBAL_HOST,
        )

    def test_paused_request_returns_the_unmodified_upstream_response(self):
        conn = mock.Mock()
        tls = mock.Mock()
        tls.makefile.return_value = io.BytesIO(
            b"POST /clls/wloc HTTP/1.1\r\n"
            b"Host: gs-loc.apple.com\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        upstream_body = b"real-location-response"
        with (
            mock.patch.object(interceptor, "modification_is_active", return_value=False),
            mock.patch.object(
                interceptor,
                "fetch_upstream",
                return_value=(
                    "HTTP/1.1 200 OK",
                    ["Content-Type: application/octet-stream"],
                    {"content-type": "application/octet-stream"},
                    upstream_body,
                ),
            ),
            mock.patch.object(
                interceptor, "active_target", side_effect=AssertionError("rewrote")
            ),
            mock.patch.object(interceptor, "log") as log,
        ):
            interceptor.handle(conn, ("127.0.0.1", 12345), context)

        response = tls.sendall.call_args.args[0]
        self.assertTrue(response.endswith(upstream_body))
        self.assertIn(b"Content-Length: 22", response)
        self.assertTrue(any(
            "MODIFIER_PAUSED_PASSTHRU" in call.args[0]
            for call in log.call_args_list
        ))

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

    def test_recent_wifi_cache_is_memory_only_bounded_and_expires(self):
        with (
            mock.patch.object(interceptor, "RECENT_WIFI_MAX", 2),
            mock.patch.object(interceptor, "RECENT_WIFI_TTL", 10),
        ):
            interceptor._remember_wifi_values(
                ["00:11:22:33:44:51", "00:11:22:33:44:52", "00:11:22:33:44:53"],
                now=1,
            )
            self.assertEqual(
                interceptor.recent_wifi_values(now=2),
                [0x001122334452, 0x001122334453],
            )
            self.assertEqual(interceptor.recent_wifi_values(now=12), [])

    def test_complete_wifi_template_is_translated_then_expires(self):
        payload = build_tile([(34.0, -118.0), (34.001, -117.999)])
        with mock.patch.object(interceptor, "WIFI_TEMPLATE_TTL", 10):
            self.assertEqual(interceptor.remember_wifi_template(payload, now=1), 2)
            replacement, count, anchor = interceptor.build_template_coverage_tile(
                -80.4167, 77.1167, now=2
            )
            self.assertEqual(count, 2)
            self.assertIsNotNone(anchor)
            self.assertEqual(len(wx.decode_locations(replacement)), 2)
            self.assertIsNone(interceptor.recent_wifi_template(now=12))

    def test_seed_request_replaces_duplicate_tile_keys(self):
        captured = {}

        def fake_fetch(host, request_line, header_lines, body):
            captured["lines"] = header_lines
            return (
                "HTTP/1.1 200 OK",
                ["Content-Type: application/octet-stream"],
                {"content-type": "application/octet-stream"},
                b"tile",
            )

        with mock.patch.object(interceptor, "fetch_upstream", side_effect=fake_fetch):
            interceptor.fetch_seed_wifi_template(
                interceptor.WIFI_TILE_GLOBAL_HOST,
                "GET /wifi_request_tile HTTP/1.1",
                ["X-tilekey: 1", "X-Test: keep", "X-tilekey: 2"],
                b"",
            )
        tile_lines = [
            line for line in captured["lines"]
            if line.lower().startswith("x-tilekey:")
        ]
        self.assertEqual(tile_lines, ["X-tilekey: %s" % interceptor.WIFI_SEED_TILEKEY])
        self.assertIn("X-Test: keep", captured["lines"])

    def test_wifi_tile_404_falls_back_to_recent_real_identities(self):
        interceptor._remember_wifi_values(
            ["00:11:22:33:44:51", "00:11:22:33:44:52"]
        )
        conn = mock.Mock()
        tls = mock.Mock()
        tls.makefile.return_value = io.BytesIO(
            b"GET /wifi_request_tile HTTP/1.1\r\n"
            b"Host: gspe85-ssl.ls.apple.com\r\n"
            b"X-tilekey: 999\r\n\r\n"
        )
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        with (
            mock.patch.object(interceptor, "modification_is_active", return_value=True),
            mock.patch.object(
                interceptor,
                "fetch_upstream",
                return_value=(
                    "HTTP/1.1 404 Not Found",
                    ["Content-Length: 0"],
                    {"content-length": "0"},
                    b"",
                ),
            ),
            mock.patch.object(
                interceptor, "fetch_seed_wifi_template", side_effect=OSError("offline")
            ),
            mock.patch.object(
                interceptor, "active_target", return_value=(-80.4167, 77.1167, 25)
            ),
            mock.patch.object(interceptor, "log"),
        ):
            interceptor.handle(conn, ("127.0.0.1", 12345), context)

        response = tls.sendall.call_args.args[0]
        head, payload = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200 OK", head)
        self.assertEqual(len(wx.decode_locations(payload)), 2)

    def test_wifi_tile_404_seed_preserves_gzip_transport(self):
        seed = build_tile([(51.48, -3.18), (51.481, -3.179)])
        conn = mock.Mock()
        tls = mock.Mock()
        tls.makefile.return_value = io.BytesIO(
            b"GET /wifi_request_tile HTTP/1.1\r\n"
            b"Host: gspe85-ssl.ls.apple.com\r\n"
            b"X-tilekey: 999\r\n\r\n"
        )
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        with (
            mock.patch.object(interceptor, "modification_is_active", return_value=True),
            mock.patch.object(
                interceptor,
                "fetch_upstream",
                return_value=(
                    "HTTP/1.1 404 Not Found",
                    ["Content-Length: 0"],
                    {"content-length": "0"},
                    b"",
                ),
            ),
            mock.patch.object(
                interceptor,
                "fetch_seed_wifi_template",
                return_value=(
                    "HTTP/1.1 200 OK",
                    [
                        "Content-Type: application/octet-stream",
                        "Content-Encoding: gzip",
                    ],
                    {
                        "content-type": "application/octet-stream",
                        "content-encoding": "gzip",
                    },
                    gzip.compress(seed, mtime=0),
                ),
            ),
            mock.patch.object(
                interceptor, "active_target", return_value=(-80.4167, 77.1167, 25)
            ),
            mock.patch.object(interceptor, "log"),
        ):
            interceptor.handle(conn, ("127.0.0.1", 12345), context)

        response = tls.sendall.call_args.args[0]
        head, payload = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200 OK", head)
        self.assertIn(b"Content-Encoding: gzip", head)
        self.assertEqual(len(wx.decode_locations(gzip.decompress(payload))), 2)


def build_tile(points):
    devices = bytearray()
    for lat, lon in points:
        location = (
            gx.tag(wx.LOCATION_LAT_FIELD, gx.WIRE_I32)
            + wx._coordinate_bytes(lat)
            + gx.tag(wx.LOCATION_LON_FIELD, gx.WIRE_I32)
            + wx._coordinate_bytes(lon)
        )
        device = gx.len_field(wx.DEVICE_LOCATION_FIELD, location)
        devices += gx.len_field(wx.REGION_DEVICE_FIELD, device)
    return gx.len_field(wx.REGION_FIELD, bytes(devices))


if __name__ == "__main__":
    unittest.main()
