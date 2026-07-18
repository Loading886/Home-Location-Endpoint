import math
import unittest
from unittest import mock

from home_location_endpoint import gsloc_rewrite as gx
from home_location_endpoint import wifitile_rewrite as wx


class WlocRewriteTests(unittest.TestCase):
    def test_parser_rejects_excessive_field_counts(self):
        message = gx.tag(1, gx.WIRE_VARINT) + gx.write_varint(1)
        with mock.patch.object(gx, "MAX_PROTOBUF_FIELDS", 2):
            with self.assertRaisesRegex(ValueError, "too many fields"):
                gx.parse_fields(message * 3)

    def test_translation_preserves_batch_geometry(self):
        first = gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_location(34.0000, -118.0000, 30))
        second = gx.build_wifi("aa:bb:cc:dd:ee:02", gx.build_location(34.0010, -117.9980, 40))
        body = gx.build_response([first, second])
        replacement, count, anchor, source = gx.translate_response(body, 40.0, -74.0)
        decoded = gx.decode_response(replacement)

        self.assertEqual(count, 2)
        self.assertEqual(source, "response-median")
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(decoded[1]["lat"] - decoded[0]["lat"], 0.001, places=7)
        expected_lon_delta = 0.002 * math.cos(math.radians(34.0005)) / math.cos(math.radians(40.0))
        self.assertAlmostEqual(decoded[1]["lon"] - decoded[0]["lon"], expected_lon_delta, places=7)
        _header, block = gx.split_response(replacement)
        self.assertEqual(gx.header_block_length(replacement[:10]), len(block))

    def test_all_sentinel_batch_becomes_centered_non_degenerate_cluster(self):
        target = (40.0, -74.0)
        body = gx.build_response([
            gx.build_wifi(
                "aa:bb:cc:dd:ee:%02x" % index,
                gx.build_sentinel_location(-1),
            )
            for index in range(24)
        ])
        replacement, count, anchor, source = gx.translate_response(
            body, *target, accuracy=25
        )
        repeated = gx.translate_response(body, *target, accuracy=25)[0]
        points = [
            (entry["lat"], entry["lon"])
            for entry in gx.decode_response(replacement)
        ]
        self.assertEqual((count, anchor, source), (24, None, gx.NO_FIX_SOURCE))
        self.assertEqual(replacement, repeated)
        self.assertEqual(len(set(points)), 24)
        self.assertLessEqual(
            max(distance_m(*target, *point) for point in points), 45.01
        )
        center = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        self.assertLess(distance_m(*target, *center), 0.02)

    def test_single_sentinel_becomes_exact_target(self):
        target = (40.0, -74.0)
        body = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_sentinel_location(-1))
        ])
        replacement, count, anchor, source = gx.translate_response(
            body, *target, accuracy=25
        )
        entry = gx.decode_response(replacement)[0]
        self.assertEqual((count, anchor, source), (1, None, gx.NO_FIX_SOURCE))
        self.assertAlmostEqual(entry["lat"], target[0], places=7)
        self.assertAlmostEqual(entry["lon"], target[1], places=7)
        self.assertEqual(entry["accuracy"], 25)

    def test_sentinel_cell_gets_plausible_cell_accuracy(self):
        target = (40.0, -74.0)
        body = gx.build_cell_response([
            gx.build_cell(gx.build_sentinel_location(-1), 460, 1, 12345, 77)
        ])
        replacement, count, anchor, source = gx.translate_response(
            body, *target, accuracy=25
        )
        entry = gx.decode_response(replacement)[0]
        self.assertEqual((count, anchor, source), (1, None, gx.NO_FIX_SOURCE))
        self.assertAlmostEqual(entry["lat"], target[0], places=7)
        self.assertAlmostEqual(entry["lon"], target[1], places=7)
        self.assertEqual(entry["accuracy"], 1000)

    def test_empty_response_is_safe_no_fix_passthrough(self):
        body = b"\x00\x01\x00\x00\x00\x01\x00\x00\x00\x00"
        replacement, count, anchor, source = gx.translate_response(
            body, 40.0, -74.0, accuracy=25
        )
        self.assertEqual(replacement, body)
        self.assertEqual((count, anchor, source), (0, None, gx.NO_FIX_SOURCE))

    def test_malformed_unanchored_location_fails_closed(self):
        malformed = gx.tag(1, gx.WIRE_VARINT) + gx.encode_int64(
            gx.js_round(34.0 * 1e8)
        )
        body = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:ff", malformed)
        ])
        with self.assertRaisesRegex(ValueError, "no safe translation anchor"):
            gx.translate_response(body, 40.0, -74.0, accuracy=25)

    def test_smooth_jitter_is_bounded_and_continuous(self):
        seed = b"x" * 32
        center = (34.0, -118.0)
        before = gx.smooth_jitter_target(*center, 8, 120, seed, "test", timestamp=119.999)
        after = gx.smooth_jitter_target(*center, 8, 120, seed, "test", timestamp=120.001)
        self.assertLess(distance_m(*before, *after), 0.1)
        self.assertLessEqual(distance_m(*center, *before), 8.01)
        self.assertLessEqual(distance_m(*center, *after), 8.01)

    def test_only_wgs84_presets_are_accepted(self):
        presets = {
            "active": "one",
            "presets": {"one": {"lat": 1, "lon": 2, "datum": "gcj02"}},
        }
        with self.assertRaises(ValueError):
            gx.resolve_target(presets)

    def test_target_rejects_nonfinite_coordinates(self):
        presets = {
            "active": "bad",
            "presets": {
                "bad": {
                    "lat": "nan",
                    "lon": 0,
                    "datum": "wgs84",
                    "accuracy_m": 25,
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "latitude"):
            gx.resolve_target(presets)


class WifiTileRewriteTests(unittest.TestCase):
    def test_wifi_tile_geometry_is_translated(self):
        payload = build_tile([(34.0, -118.0), (34.002, -117.997)])
        replacement, count, anchor = wx.translate_wifi_tile(payload, 40.0, -74.0)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 2)
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(points[1][0] - points[0][0], 0.002, places=6)
        self.assertNotEqual(points[0], points[1])

    def test_wifi_tile_no_fix_marker_is_preserved_not_translated(self):
        # A tile mixing a real AP with a (-180,-180) no-fix marker must not raise
        # (translate_coordinate would reject the marker) and must not fabricate a
        # fix for it: only the valid AP is translated, the marker is left as-is.
        payload = build_tile([(34.0, -118.0), (-180.0, -180.0)])
        replacement, count, anchor = wx.translate_wifi_tile(payload, 40.0, -74.0)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 1)
        self.assertTrue(35.0 <= points[0][0] <= 45.0)
        self.assertEqual(points[1], (-180.0, -180.0))

    def test_wifi_tile_out_of_range_marker_is_not_fabricated(self):
        payload = build_tile([(33.9, -118.0), (34.1, -118.0), (-95.0, -118.05)])
        replacement, count, _anchor = wx.translate_wifi_tile(payload, 40.0, -74.0)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(points[2][0], -95.0, places=6)
        self.assertAlmostEqual(points[2][1], -118.05, places=6)


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


def distance_m(lat1, lon1, lat2, lon2):
    north = math.radians(lat2 - lat1) * gx.EARTH_RADIUS_M
    east = (
        math.radians(lon2 - lon1)
        * gx.EARTH_RADIUS_M
        * math.cos(math.radians((lat1 + lat2) / 2))
    )
    return math.hypot(north, east)


if __name__ == "__main__":
    unittest.main()
