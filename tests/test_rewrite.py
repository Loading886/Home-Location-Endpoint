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

    def test_translation_preserves_small_batch_geometry(self):
        first = gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_location(34.0000, -118.0000, 30))
        second = gx.build_wifi("aa:bb:cc:dd:ee:02", gx.build_location(34.0001, -117.9998, 40))
        body = gx.build_response([first, second])
        replacement, count, anchor, source = gx.translate_response(body, 40.0, -74.0)
        decoded = gx.decode_response(replacement)

        self.assertEqual(count, 2)
        self.assertEqual(source, "response-median")
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(decoded[1]["lat"] - decoded[0]["lat"], 0.0001, places=7)
        expected_lon_delta = 0.0002 * math.cos(math.radians(34.00005)) / math.cos(math.radians(40.0))
        self.assertAlmostEqual(decoded[1]["lon"] - decoded[0]["lon"], expected_lon_delta, places=7)
        _header, block = gx.split_response(replacement)
        self.assertEqual(gx.header_block_length(replacement[:10]), len(block))

    def test_far_valid_geometry_is_uniformly_compressed(self):
        target = (40.0, -74.0)
        source = (34.0, -118.0)
        north = (
            source[0] + math.degrees(1000.0 / gx.EARTH_RADIUS_M),
            source[1],
        )
        east = (
            source[0],
            source[1]
            + math.degrees(
                500.0
                / (gx.EARTH_RADIUS_M * math.cos(math.radians(source[0])))
            ),
        )
        body = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_location(*source, 25)),
            gx.build_wifi("aa:bb:cc:dd:ee:02", gx.build_location(*north, 25)),
            gx.build_wifi("aa:bb:cc:dd:ee:03", gx.build_location(*east, 25)),
        ])
        replacement, count, _anchor, _source = gx.translate_response(body, *target)
        points = [
            (entry["lat"], entry["lon"])
            for entry in gx.decode_response(replacement)
        ]
        distances = [distance_m(*target, *point) for point in points]
        self.assertEqual(count, 3)
        self.assertLessEqual(max(distances), gx.TRANSLATED_CLUSTER_RADIUS_M + 0.02)
        self.assertLess(abs(distances[1] / distances[2] - 2.0), 0.01)

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

    def test_sparse_antarctic_no_fix_uses_recent_real_wifi_identities(self):
        target = (-80.4167, 77.1167)
        body = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_sentinel_location(-1))
        ])
        recent = [0x001122330000 + index for index in range(40)]
        prepared, original, prepared_count = gx.supplement_sparse_no_fix_response(
            body, recent, minimum_locations=32
        )
        replacement, count, anchor, source = gx.translate_response(
            prepared, *target, accuracy=25
        )
        points = [
            (entry["lat"], entry["lon"])
            for entry in gx.decode_response(replacement)
        ]
        self.assertEqual((original, prepared_count), (1, 32))
        self.assertEqual((count, anchor, source), (32, None, gx.NO_FIX_SOURCE))
        self.assertEqual(len(set(points)), 32)
        self.assertLessEqual(
            max(distance_m(*target, *point) for point in points), 45.02
        )

    def test_sparse_supplement_never_changes_valid_or_empty_batches(self):
        valid = gx.build_response([
            gx.build_wifi("aa:bb:cc:dd:ee:01", gx.build_location(1, 2, 25))
        ])
        empty = gx.build_response([])
        for body, expected in ((valid, 1), (empty, 0)):
            prepared, original, prepared_count = gx.supplement_sparse_no_fix_response(
                body, [0x001122334455], minimum_locations=32
            )
            self.assertEqual(prepared, body)
            self.assertEqual((original, prepared_count), (expected, expected))

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
    def test_wifi_tile_geometry_is_translated_and_bounded(self):
        target = (40.0, -74.0)
        payload = build_tile([(34.0, -118.0), (34.009, -117.99475)])
        replacement, count, anchor = wx.translate_wifi_tile(payload, 40.0, -74.0)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 2)
        self.assertIsNotNone(anchor)
        self.assertNotEqual(points[0], points[1])
        self.assertLessEqual(
            max(distance_m(*target, *point) for point in points),
            gx.TRANSLATED_CLUSTER_RADIUS_M + 0.02,
        )

    def test_wifi_tile_small_geometry_is_not_expanded(self):
        target = (40.0, -74.0)
        second = gx.offset_coordinate(34.0, -118.0, 10.0, 5.0)
        payload = build_tile([(34.0, -118.0), second])
        replacement, count, _anchor = wx.translate_wifi_tile(payload, *target)
        points = wx.decode_locations(replacement)
        self.assertEqual(count, 2)
        self.assertLess(
            abs(distance_m(*points[0], *points[1]) - math.hypot(10.0, 5.0)),
            0.1,
        )

    def test_wifi_tile_representative_large_batch_stays_within_target_radius(self):
        source = (34.0, -118.0)
        target = (40.0, -74.0)
        points = []
        for index in range(469):
            radius = 1430.0 * ((index + 1) / 469.0) ** 0.78
            angle = index * 2.399963229728653
            north = radius * math.cos(angle)
            east = radius * math.sin(angle)
            points.append((
                source[0] + math.degrees(north / gx.EARTH_RADIUS_M),
                source[1]
                + math.degrees(
                    east
                    / (gx.EARTH_RADIUS_M * math.cos(math.radians(source[0])))
                ),
            ))

        replacement, count, _anchor = wx.translate_wifi_tile(
            build_tile(points), *target
        )
        translated = wx.decode_locations(replacement)
        self.assertEqual(count, 469)
        self.assertEqual(len(translated), 469)
        self.assertLessEqual(
            max(distance_m(*target, *point) for point in translated),
            gx.TRANSLATED_CLUSTER_RADIUS_M + 0.02,
        )

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

    def test_synthetic_antarctic_tile_is_centered_bounded_and_deduplicated(self):
        target = (-80.4167, 77.1167)
        payload, count = wx.build_synthetic_wifi_tile(
            [0x001122334455, 0x001122334456, 0x001122334455, "invalid"],
            *target,
        )
        points = wx.decode_locations(payload)
        self.assertEqual(count, 2)
        self.assertEqual(len(set(points)), 2)
        self.assertLessEqual(
            max(distance_m(*target, *point) for point in points), 45.02
        )
        center = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        self.assertLess(distance_m(*target, *center), 0.02)


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
