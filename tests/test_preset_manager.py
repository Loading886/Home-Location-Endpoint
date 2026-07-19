import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from home_location_endpoint import gsloc_rewrite, location_picker, preset_manager


ROOT = Path(__file__).resolve().parents[1]


class PresetManagerTests(unittest.TestCase):
    def base_config(self):
        info = {
            "ip": "203.0.113.9",
            "city": "Test City",
            "region": "Test Region",
            "country": "Test Country",
            "country_code": "TC",
            "latitude": 10.0,
            "longitude": 20.0,
            "timezone": "Etc/UTC",
        }
        return location_picker.build_config(info, 10.01, 20.01, "city-boundary")

    def catalog(self):
        return json.loads(
            (ROOT / "configs" / "advanced-location-catalog.json").read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def distance_m(lat1, lon1, lat2, lon2):
        radius = 6_371_008.8
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        value = (
            math.sin(dp / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        )
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))

    def test_catalog_presets_are_randomized_inside_conservative_radius(self):
        catalog = self.catalog()
        first = preset_manager.build_advanced_config(
            self.base_config(), catalog, rng=random.Random(1)
        )
        second = preset_manager.build_advanced_config(
            self.base_config(), catalog, rng=random.Random(2)
        )
        self.assertEqual(len(first["presets"]), 10)
        self.assertNotEqual(
            first["presets"]["tokyo"]["lat"], second["presets"]["tokyo"]["lat"]
        )
        self.assertEqual(first["presets"]["ip_city"]["menu_label"], "🌐 出口城市")
        for key, definition in catalog["presets"].items():
            point = first["presets"][key]
            distance = self.distance_m(
                definition["center_lat"], definition["center_lon"],
                point["lat"], point["lon"],
            )
            self.assertLessEqual(distance, definition["random_radius_m"] + 0.5)

    def test_antarctic_preset_disables_smooth_jitter(self):
        config = preset_manager.build_advanced_config(
            self.base_config(), self.catalog(), rng=random.Random(4)
        )
        preset = config["presets"]["kunlun_station"]
        self.assertEqual(preset["jitter"], {"enabled": False})
        self.assertEqual(
            gsloc_rewrite.resolve_jitter(config, "kunlun_station"),
            (0.0, config["jitter"]["period_s"]),
        )

    def test_per_preset_jitter_is_validated(self):
        config = self.base_config()
        config["presets"]["ip_city"]["jitter"] = {"enabled": "no"}
        with self.assertRaisesRegex(preset_manager.PresetError, "布尔值"):
            preset_manager.validate(config)

    def test_add_switch_delete_and_modifier_state_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "location.json"
            backup = root / "backups"
            state = root / "modifier.state"
            config = preset_manager.build_advanced_config(
                self.base_config(), self.catalog(), rng=random.Random(3)
            )
            preset_manager.atomic_write(path, config)
            state.write_text("active\n", encoding="ascii")

            key, saved = preset_manager.add(
                path, backup, "🇺🇸 Test", "Test address", 11.0, 21.0
            )
            self.assertIn(key, saved["presets"])
            entry, saved = preset_manager.set_active(path, backup, key)
            self.assertEqual(saved["active"], key)
            self.assertEqual(entry["lat"], 11.0)
            with self.assertRaises(preset_manager.PresetError):
                preset_manager.delete(path, backup, key)
            preset_manager.set_active(path, backup, "ip_city")
            removed, saved = preset_manager.delete(path, backup, key)
            self.assertEqual(removed["menu_label"], "🇺🇸 Test")
            self.assertNotIn(key, saved["presets"])
            preset_manager.write_modifier_state(state, "paused")
            self.assertEqual(state.read_text(encoding="ascii"), "paused\n")
            self.assertTrue(list(backup.glob("location.json.*.bak")))

    def test_preset_count_is_bounded_for_telegram_keyboards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "location.json"
            backup = root / "backups"
            config = self.base_config()
            template = next(iter(config["presets"].values()))
            for index in range(1, preset_manager.MAX_PRESETS):
                config["presets"]["preset_%02d" % index] = dict(template)
            preset_manager.atomic_write(path, config)

            with self.assertRaisesRegex(preset_manager.PresetError, "上限"):
                preset_manager.add(
                    path, backup, "Limit Test", "Test address", 11.0, 21.0
                )

            config["presets"]["preset_overflow"] = dict(template)
            with self.assertRaisesRegex(preset_manager.PresetError, "不能超过"):
                preset_manager.validate(config)


if __name__ == "__main__":
    unittest.main()
