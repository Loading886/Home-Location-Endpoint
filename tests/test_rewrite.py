import math
import unittest

from home_location_endpoint import gsloc_rewrite as gx
from home_location_endpoint import wifitile_rewrite as wx


class WlocRewriteTests(unittest.TestCase):
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

    def test_no_fix_batch_is_not_fabricated(self):
        body = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_sentinel_location(100))
        ])
        replacement, count, anchor, source = gx.translate_response(body, 40.0, -74.0)
        self.assertEqual(replacement, body)
        self.assertEqual(count, 0)
        self.assertIsNone(anchor)
        self.assertEqual(source, gx.NO_FIX_SOURCE)

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


class WifiTileRewriteTests(unittest.TestCase):
    def test_wifi_tile_geometry_is_translated(self):
        payload = build_tile([(34.0, -118.0), (34.002, -117.997)])
        replacement, count, anchor = wx.translate_wifi_tile(payload, 40.0, -74.0)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 2)
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(points[1][0] - points[0][0], 0.002, places=6)
        self.assertNotEqual(points[0], points[1])


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
