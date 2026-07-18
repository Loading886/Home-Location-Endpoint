import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from home_location_endpoint import location_picker as picker


IP_DATA = {
    "success": True,
    "ip": "8.8.8.8",
    "city": "Example City",
    "region": "Example State",
    "country": "United States",
    "country_code": "US",
    "latitude": 34.0000,
    "longitude": -118.0000,
    "timezone": {"id": "America/Los_Angeles"},
}

SQUARE = {
    "type": "Polygon",
    "coordinates": [[
        [-118.10, 33.90],
        [-117.90, 33.90],
        [-117.90, 34.10],
        [-118.10, 34.10],
        [-118.10, 33.90],
    ]],
}


class LocationPickerTests(unittest.TestCase):
    def test_samples_inside_city_boundary(self):
        info, lat, lon, method = picker.select_location(
            IP_DATA, geometry=SQUARE, rng=random.Random(7)
        )
        self.assertEqual(info["city"], "Example City")
        self.assertEqual(method, "city-boundary")
        self.assertTrue(picker.geometry_contains(SQUARE, lat, lon))

    def test_new_seed_selects_a_new_point(self):
        first = picker.select_location(IP_DATA, geometry=SQUARE, rng=random.Random(1))
        second = picker.select_location(IP_DATA, geometry=SQUARE, rng=random.Random(2))
        self.assertNotEqual(first[1:3], second[1:3])

    def test_polygon_hole_is_excluded(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [
                SQUARE["coordinates"][0],
                [
                    [-118.01, 33.99],
                    [-117.99, 33.99],
                    [-117.99, 34.01],
                    [-118.01, 34.01],
                    [-118.01, 33.99],
                ],
            ],
        }
        self.assertFalse(picker.geometry_contains(geometry, 34.0, -118.0))
        self.assertTrue(picker.geometry_contains(geometry, 34.05, -118.05))

    def test_fallback_stays_in_requested_radius(self):
        _info, lat, lon, method = picker.select_location(
            IP_DATA, geometry=None, fallback_radius_m=3000, rng=random.Random(11)
        )
        self.assertEqual(method, "ip-center-radius-fallback")
        distance = haversine_m(34.0, -118.0, lat, lon)
        self.assertLessEqual(distance, 3000.01)

    def test_config_records_method_without_exposing_secrets(self):
        info = picker.validate_ip_location(IP_DATA)
        config = picker.build_config(info, 34.01, -118.02, "city-boundary")
        self.assertEqual(config["active"], "ip_city")
        self.assertEqual(config["presets"]["ip_city"]["datum"], "wgs84")
        self.assertEqual(config["jitter"]["radius_m"], 8)
        self.assertNotIn("token", json.dumps(config).lower())

    def test_atomic_cache_round_trip(self):
        info = picker.validate_ip_location(IP_DATA)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            picker.atomic_json(
                path,
                {"key": picker.city_cache_key(info), "geometry": SQUARE},
                mode=0o644,
            )
            self.assertEqual(picker.load_cached_geometry(path, info), SQUARE)

    def test_rejects_unusable_ip_response(self):
        broken = dict(IP_DATA)
        broken["city"] = ""
        with self.assertRaises(ValueError):
            picker.validate_ip_location(broken)

    def test_rejects_private_provider_address(self):
        broken = dict(IP_DATA)
        broken["ip"] = "10.0.0.1"
        with self.assertRaises(ValueError):
            picker.validate_ip_location(broken)

    def test_rejects_invalid_country_code(self):
        broken = dict(IP_DATA)
        broken["country_code"] = "1!"
        with self.assertRaises(ValueError):
            picker.validate_ip_location(broken)

    def test_rejects_provider_control_characters(self):
        broken = dict(IP_DATA)
        broken["city"] = "Example\x1b[31mCity"
        with self.assertRaisesRegex(ValueError, "control characters"):
            picker.validate_ip_location(broken)

    def test_malformed_geometry_falls_back_safely(self):
        malformed = {
            "type": "Polygon",
            "coordinates": [[["not-a-longitude", 34], [0, 0], [0, 1], [0, 0]]],
        }
        self.assertFalse(picker._is_polygon(malformed))
        _info, _lat, _lon, method = picker.select_location(
            IP_DATA, geometry=malformed, rng=random.Random(5)
        )
        self.assertEqual(method, "ip-center-radius-fallback")


def haversine_m(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    )
    return 2 * picker.EARTH_RADIUS_M * math.asin(math.sqrt(value))


if __name__ == "__main__":
    unittest.main()
