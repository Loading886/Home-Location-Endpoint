#!/usr/bin/env python3
"""Validate and atomically mutate Telegram-managed location presets."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path


EARTH_RADIUS_M = 6_371_008.8
KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_PRESETS = 50


class PresetError(ValueError):
    pass


def _number(value, name, low, high):
    if isinstance(value, bool):
        raise PresetError("%s must be a number" % name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PresetError("%s must be a number" % name) from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise PresetError("%s must be between %s and %s" % (name, low, high))
    return number


def validate_menu_label(value):
    value = " ".join(str(value or "").strip().split())
    if not 2 <= len(value) <= 40:
        raise PresetError("地点名称长度需为 2-40 个字符")
    return value


def validate_address(value):
    value = " ".join(str(value or "").strip().split())
    if not 2 <= len(value) <= 200:
        raise PresetError("地址长度需为 2-200 个字符")
    return value


def parse_coordinates(value):
    parts = re.split(r"\s*[,，]\s*|\s+", str(value or "").strip())
    parts = [item for item in parts if item]
    if len(parts) != 2:
        raise PresetError("坐标格式应为：纬度, 经度")
    return (
        _number(parts[0], "纬度", -90, 90),
        _number(parts[1], "经度", -180, 180),
    )


def _validate_jitter(value, name):
    if not isinstance(value, dict):
        raise PresetError("%s 必须是对象" % name)
    if set(value) - {"enabled", "radius_m", "period_s"}:
        raise PresetError("%s 包含未知字段" % name)
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise PresetError("%s.enabled 必须是布尔值" % name)
    if "radius_m" in value:
        _number(value["radius_m"], "%s.radius_m" % name, 0, 100)
    if "period_s" in value:
        _number(value["period_s"], "%s.period_s" % name, 30, 3600)


def validate(data):
    if not isinstance(data, dict):
        raise PresetError("地点配置必须是对象")
    presets = data.get("presets")
    active = data.get("active")
    if not isinstance(presets, dict) or not presets:
        raise PresetError("至少需要保留一个地点")
    if len(presets) > MAX_PRESETS:
        raise PresetError("地点数量不能超过 %d 个" % MAX_PRESETS)
    if not isinstance(active, str) or active not in presets:
        raise PresetError("active 必须指向现有地点")
    _number(data.get("default_accuracy_m", 25), "default_accuracy_m", 0.1, 100000)
    _validate_jitter(data.get("jitter", {}), "jitter")
    for key, entry in presets.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise PresetError("地点键不合法：%r" % key)
        if not isinstance(entry, dict):
            raise PresetError("地点 %s 必须是对象" % key)
        validate_menu_label(entry.get("menu_label") or entry.get("label"))
        validate_address(entry.get("address", "Unknown location"))
        _number(entry.get("lat"), "%s.lat" % key, -90, 90)
        _number(entry.get("lon"), "%s.lon" % key, -180, 180)
        if str(entry.get("datum", "wgs84")).lower() != "wgs84":
            raise PresetError("地点 %s 必须使用 WGS84 坐标" % key)
        _number(
            entry.get("accuracy_m", data.get("default_accuracy_m", 25)),
            "%s.accuracy_m" % key,
            0.1,
            100000,
        )
        if "jitter" in entry:
            _validate_jitter(entry["jitter"], "%s.jitter" % key)
    return data


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PresetError("无法读取地点配置：%s" % exc) from exc
    return validate(data)


def random_point_near(lat, lon, radius_m, rng=None):
    rng = rng or secrets.SystemRandom()
    lat = _number(lat, "center_lat", -90, 90)
    lon = _number(lon, "center_lon", -180, 180)
    radius_m = _number(radius_m, "random_radius_m", 1, 50000)
    distance = radius_m * math.sqrt(rng.random())
    bearing = rng.random() * 2 * math.pi
    angular = distance / EARTH_RADIUS_M
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular)
        + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 8), round(
        ((math.degrees(lon2) + 180) % 360) - 180, 8
    )


def build_advanced_config(base_config, catalog, rng=None):
    validate(base_config)
    entries = catalog.get("presets") if isinstance(catalog, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise PresetError("预置地点目录为空")
    output = copy.deepcopy(base_config)
    output["schema"] = max(2, int(output.get("schema", 1)))
    output["managed_by"] = "home-location-telegram-bot"
    source_city = str(output.get("source", {}).get("city") or "Egress city")
    source = output["presets"][output["active"]]
    source["menu_label"] = "🌐 %s" % source_city
    source["address"] = "%s (automatic egress-city point)" % source_city
    for key, entry in entries.items():
        if not KEY_RE.fullmatch(str(key)) or key in output["presets"]:
            raise PresetError("预置地点键重复或不合法：%s" % key)
        if not isinstance(entry, dict):
            raise PresetError("预置地点 %s 必须是对象" % key)
        lat, lon = random_point_near(
            entry.get("center_lat"),
            entry.get("center_lon"),
            entry.get("random_radius_m"),
            rng=rng,
        )
        preset = {
            "label": validate_menu_label(entry.get("menu_label")),
            "menu_label": validate_menu_label(entry.get("menu_label")),
            "address": validate_address(entry.get("address")),
            "lat": lat,
            "lon": lon,
            "accuracy_m": 25,
            "datum": "wgs84",
            "randomized_at_install": True,
        }
        if "jitter" in entry:
            _validate_jitter(entry["jitter"], "%s.jitter" % key)
            preset["jitter"] = copy.deepcopy(entry["jitter"])
        output["presets"][key] = preset
    return validate(output)


def _backup(path, backup_dir, keep=30):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = backup_dir / ("%s.%s.%d.bak" % (path.name, stamp, time.time_ns()))
    shutil.copy2(path, target)
    for old in sorted(backup_dir.glob("%s.*.bak" % path.name))[:-keep]:
        old.unlink()


def atomic_write(path, data, backup_dir=None):
    path = Path(path)
    original = path.stat() if path.exists() else None
    if original is not None and backup_dir is not None:
        _backup(path, Path(backup_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=str(path.parent), prefix=".location-")
    temporary = Path(name)
    try:
        mode = stat.S_IMODE(original.st_mode) if original else 0o640
        os.fchmod(descriptor, mode)
        if original is not None and hasattr(os, "fchown"):
            os.fchown(descriptor, original.st_uid, original.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mutate(path, backup_dir, callback):
    data = load(path)
    result = callback(data)
    validate(data)
    atomic_write(path, data, backup_dir)
    return result, data


def set_active(path, backup_dir, key):
    key = str(key or "")
    if not KEY_RE.fullmatch(key):
        raise PresetError("地点键不合法")

    def change(data):
        if key not in data["presets"]:
            raise PresetError("地点不存在")
        data["active"] = key
        return data["presets"][key]

    return _mutate(path, backup_dir, change)


def _new_key(presets):
    base = "custom_%s" % time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    key = base
    suffix = 2
    while key in presets:
        key = "%s_%d" % (base, suffix)
        suffix += 1
    return key


def add(path, backup_dir, menu_label, address, lat, lon):
    menu_label = validate_menu_label(menu_label)
    address = validate_address(address)
    lat = _number(lat, "纬度", -90, 90)
    lon = _number(lon, "经度", -180, 180)

    def change(data):
        if len(data["presets"]) >= MAX_PRESETS:
            raise PresetError("地点数量已达 %d 个上限" % MAX_PRESETS)
        key = _new_key(data["presets"])
        data["presets"][key] = {
            "label": menu_label,
            "menu_label": menu_label,
            "address": address,
            "lat": lat,
            "lon": lon,
            "accuracy_m": 25,
            "datum": "wgs84",
            "added_via": "telegram",
        }
        return key

    return _mutate(path, backup_dir, change)


def delete(path, backup_dir, key):
    key = str(key or "")
    if not KEY_RE.fullmatch(key):
        raise PresetError("地点键不合法")

    def change(data):
        if key not in data["presets"]:
            raise PresetError("地点不存在")
        if data["active"] == key:
            raise PresetError("不能删除当前地点，请先切换")
        if len(data["presets"]) <= 1:
            raise PresetError("不能删除最后一个地点")
        return data["presets"].pop(key)

    return _mutate(path, backup_dir, change)


def write_modifier_state(path, value):
    if value not in {"active", "paused"}:
        raise PresetError("定位状态不合法")
    path = Path(path)
    original = path.stat()
    descriptor, name = tempfile.mkstemp(dir=str(path.parent), prefix=".modifier-")
    temporary = Path(name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(original.st_mode))
        if hasattr(os, "fchown"):
            os.fchown(descriptor, original.st_uid, original.st_gid)
        os.write(descriptor, (value + "\n").encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
