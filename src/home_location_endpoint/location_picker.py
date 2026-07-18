#!/usr/bin/env python3
"""Select and persist a random WGS84 point in the server IP's city."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import math
import os
import random
import re
import secrets
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_IP_API = "https://ipwho.is/"
DEFAULT_GEOCODER = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Home-Location-Endpoint/0.1 (+https://github.com/Loading886/Home-Location-Endpoint)"
MAX_IP_RESPONSE = 1 * 1024 * 1024
MAX_GEOCODER_RESPONSE = 16 * 1024 * 1024
MAX_GEOMETRY_POINTS = 500_000
EARTH_RADIUS_M = 6_371_008.8
TIMEZONE_RE = re.compile(
    r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$"
)


def _read_limited(response, limit):
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("provider response exceeds %d bytes" % limit)
    return body


def fetch_json(url, *, timeout=15, limit=MAX_IP_RESPONSE, attempts=3):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("provider URL must use HTTPS")
    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise ValueError("provider redirected away from HTTPS")
                if response.status != 200:
                    raise ValueError("provider returned HTTP %d" % response.status)
                return json.loads(_read_limited(response, limit).decode("utf-8"))
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise ValueError("provider request failed after %d attempts" % attempts) from last_error


def validate_provider_text(value, name, *, maximum, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise ValueError("IP provider did not return a usable %s" % name)
    if len(text) > maximum:
        raise ValueError("IP provider returned an overlong %s" % name)
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ValueError("IP provider returned control characters in %s" % name)
    return text


def validate_ip_location(data):
    if not isinstance(data, dict) or data.get("success") is False:
        raise ValueError("IP geolocation lookup failed")
    ip = str(data.get("ip", "")).strip()
    parsed_ip = ipaddress.ip_address(ip)
    if not parsed_ip.is_global:
        raise ValueError("IP provider did not return a public egress address")
    city = validate_provider_text(data.get("city"), "city", maximum=200, required=True)
    country_code = str(data.get("country_code", "")).strip().upper()
    if (
        len(country_code) != 2
        or not country_code.isascii()
        or not country_code.isalpha()
    ):
        raise ValueError("IP provider did not return a usable city/country")
    lat_raw = data.get("latitude")
    lon_raw = data.get("longitude")
    if (
        lat_raw is None
        or lon_raw is None
        or isinstance(lat_raw, bool)
        or isinstance(lon_raw, bool)
    ):
        raise ValueError("IP provider returned an invalid coordinate")
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("IP provider returned an invalid coordinate") from exc
    if not (
        math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    ):
        raise ValueError("IP provider returned an invalid coordinate")
    timezone = data.get("timezone", {})
    if isinstance(timezone, dict):
        timezone = timezone.get("id", "")
    timezone = validate_provider_text(timezone, "timezone", maximum=128)
    if timezone and not TIMEZONE_RE.fullmatch(timezone):
        raise ValueError("IP provider returned an invalid timezone")
    return {
        "ip": ip,
        "city": city,
        "region": validate_provider_text(data.get("region"), "region", maximum=200),
        "country": validate_provider_text(data.get("country"), "country", maximum=200),
        "country_code": country_code,
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
    }


def city_cache_key(info):
    return "|".join(
        value.casefold()
        for value in (info["city"], info["region"], info["country_code"])
    )


def _is_polygon(geometry):
    if not isinstance(geometry, dict):
        return False
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type not in {"Polygon", "MultiPolygon"} or not isinstance(
        coordinates, list
    ):
        return False
    polygons = [coordinates] if geometry_type == "Polygon" else coordinates
    point_count = 0
    try:
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                return False
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4:
                    return False
                for point in ring:
                    if not isinstance(point, (list, tuple)) or len(point) < 2:
                        return False
                    lon, lat = float(point[0]), float(point[1])
                    if not (
                        math.isfinite(lat)
                        and math.isfinite(lon)
                        and -90 <= lat <= 90
                        and -180 <= lon <= 180
                    ):
                        return False
                    point_count += 1
                    if point_count > MAX_GEOMETRY_POINTS:
                        return False
    except (TypeError, ValueError, OverflowError):
        return False
    return point_count > 0


def choose_city_geometry(results, info):
    if not isinstance(results, list):
        return None
    candidates = []
    for result in results:
        if not isinstance(result, dict) or not _is_polygon(result.get("geojson")):
            continue
        address = result.get("address") or {}
        if not isinstance(address, dict):
            continue
        result_country = str(address.get("country_code", "")).upper()
        if result_country and result_country != info["country_code"]:
            continue
        try:
            rank = int(result.get("place_rank", 99))
        except (TypeError, ValueError):
            rank = 99
        addresstype = str(result.get("addresstype", ""))
        type_penalty = 0 if addresstype in {"city", "town", "municipality"} else 10
        candidates.append((type_penalty + abs(rank - 16), result["geojson"]))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def fetch_city_geometry(info, geocoder_url=DEFAULT_GEOCODER):
    params = {
        "format": "jsonv2",
        "city": info["city"],
        "state": info["region"],
        "country": info["country"],
        "countrycodes": info["country_code"].lower(),
        "addressdetails": "1",
        "polygon_geojson": "1",
        # Topology-preserving simplification keeps large metro boundaries from
        # expanding into hundreds of megabytes of Python objects on small VPSes.
        "polygon_threshold": "0.0001",
        "limit": "5",
    }
    url = geocoder_url + "?" + urllib.parse.urlencode(
        {key: value for key, value in params.items() if value}
    )
    return choose_city_geometry(
        fetch_json(url, limit=MAX_GEOCODER_RESPONSE), info
    )


def _iter_polygons(geometry):
    if geometry["type"] == "Polygon":
        yield geometry["coordinates"]
    else:
        yield from geometry["coordinates"]


def _ring_contains(lon, lat, ring):
    inside = False
    if len(ring) < 4:
        return False
    previous = ring[-1]
    for current in ring:
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def geometry_contains(geometry, lat, lon):
    for polygon in _iter_polygons(geometry):
        if not polygon or not _ring_contains(lon, lat, polygon[0]):
            continue
        if any(_ring_contains(lon, lat, hole) for hole in polygon[1:]):
            continue
        return True
    return False


def geometry_bounds(geometry):
    lats = []
    lons = []
    for polygon in _iter_polygons(geometry):
        for ring in polygon:
            for point in ring:
                if len(point) >= 2:
                    lons.append(float(point[0]))
                    lats.append(float(point[1]))
    if not lats or not lons:
        raise ValueError("city geometry contains no coordinates")
    return min(lats), max(lats), min(lons), max(lons)


def random_point_in_geometry(geometry, rng, attempts=10_000):
    min_lat, max_lat, min_lon, max_lon = geometry_bounds(geometry)
    for _ in range(attempts):
        lat = rng.uniform(min_lat, max_lat)
        lon = rng.uniform(min_lon, max_lon)
        if geometry_contains(geometry, lat, lon):
            return lat, lon
    raise ValueError("could not sample a point inside the city boundary")


def random_point_near(lat, lon, radius_m, rng):
    radius_m = float(radius_m)
    if not 100 <= radius_m <= 50_000:
        raise ValueError("fallback radius must be between 100 and 50000 metres")
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
    return math.degrees(lat2), ((math.degrees(lon2) + 180) % 360) - 180


def load_cached_geometry(path, info):
    if not path or not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if cached.get("key") != city_cache_key(info):
        return None
    geometry = cached.get("geometry")
    return geometry if _is_polygon(geometry) else None


def atomic_json(path, value, mode=0o600, *, uid=None, gid=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temporary, mode)
        if uid is not None or gid is not None:
            if not hasattr(os, "fchown"):
                raise OSError("atomic ownership changes require POSIX fchown")
            os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with handle:
            json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def select_location(
    ip_data,
    *,
    geometry=None,
    fallback_radius_m=3_000,
    rng=None,
):
    info = validate_ip_location(ip_data)
    rng = rng or secrets.SystemRandom()
    method = "city-boundary"
    if geometry is not None:
        try:
            lat, lon = random_point_in_geometry(geometry, rng)
        except (IndexError, TypeError, ValueError, OverflowError):
            geometry = None
    if geometry is None:
        method = "ip-center-radius-fallback"
        lat, lon = random_point_near(
            info["latitude"], info["longitude"], fallback_radius_m, rng
        )
    return info, lat, lon, method


def build_config(info, lat, lon, method):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": 1,
        "active": "ip_city",
        "default_accuracy_m": 25,
        "jitter": {"enabled": True, "radius_m": 8, "period_s": 120},
        "source": {
            "provider": "ipwho.is",
            "ip": info["ip"],
            "city": info["city"],
            "region": info["region"],
            "country": info["country"],
            "country_code": info["country_code"],
            "timezone": info["timezone"],
            "selection": method,
            "selected_at": now,
            "boundary_data": "OpenStreetMap contributors" if method == "city-boundary" else None,
        },
        "presets": {
            "ip_city": {
                "label": "%s random point" % info["city"],
                "lat": round(lat, 8),
                "lon": round(lon, 8),
                "accuracy_m": 25,
                "datum": "wgs84",
            }
        },
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--ip-api", default=DEFAULT_IP_API)
    parser.add_argument("--geocoder-url", default=DEFAULT_GEOCODER)
    parser.add_argument("--fallback-radius-m", type=float, default=3_000)
    parser.add_argument("--ip-json", type=Path, help="offline IP response for tests")
    parser.add_argument("--geometry-json", type=Path, help="offline GeoJSON for tests")
    parser.add_argument("--seed", type=int, help="deterministic tests only")
    parser.add_argument("--output-mode", type=lambda value: int(value, 8), default=0o600)
    parser.add_argument("--output-uid", type=int)
    parser.add_argument("--output-gid", type=int)
    return parser.parse_args()


def main():
    args = parse_args()
    if not 0 <= args.output_mode <= 0o777:
        raise SystemExit("--output-mode must be an octal mode between 000 and 777")
    if args.output_uid is not None and args.output_uid < 0:
        raise SystemExit("--output-uid must be non-negative")
    if args.output_gid is not None and args.output_gid < 0:
        raise SystemExit("--output-gid must be non-negative")
    try:
        if args.ip_json:
            ip_data = json.loads(args.ip_json.read_text(encoding="utf-8"))
        else:
            ip_data = fetch_json(args.ip_api)
        info = validate_ip_location(ip_data)
    except (ValueError, OSError) as exc:
        # Turn an unreachable provider or a malformed response into one clear
        # line instead of a multi-line Python traceback; the installer relies
        # only on the non-zero exit to fall back or fail cleanly.
        raise SystemExit("IP geolocation lookup failed: %s" % exc)
    geometry = None
    if args.geometry_json:
        geometry = json.loads(args.geometry_json.read_text(encoding="utf-8"))
    else:
        geometry = load_cached_geometry(args.cache, info)
        if geometry is None:
            try:
                geometry = fetch_city_geometry(info, args.geocoder_url)
            except Exception:
                geometry = None
            if geometry is not None and args.cache:
                atomic_json(
                    args.cache,
                    {"key": city_cache_key(info), "geometry": geometry},
                    mode=0o644,
                )
    rng = random.Random(args.seed) if args.seed is not None else secrets.SystemRandom()
    info, lat, lon, method = select_location(
        ip_data,
        geometry=geometry,
        fallback_radius_m=args.fallback_radius_m,
        rng=rng,
    )
    atomic_json(
        args.output,
        build_config(info, lat, lon, method),
        mode=args.output_mode,
        uid=args.output_uid,
        gid=args.output_gid,
    )
    print("selected %s, %s via %s" % (info["city"], info["country_code"], method))


if __name__ == "__main__":
    main()
