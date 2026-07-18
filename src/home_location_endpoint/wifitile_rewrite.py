#!/usr/bin/env python3
"""Lossless coordinate rewrite for Apple GeoServices WifiTile protobufs.

The response from ``gspe85-ssl.ls.apple.com/wifi_request_tile`` is a protobuf
whose relevant shape is:

  WifiTile.region[3].devices[2].entry[6].{lat[1], long[2]}

Both coordinates are ``sfixed32`` values in degrees * 1e7.  This module only
replaces those eight payload bytes. All BSSIDs, unknown fields, field ordering,
and container structure are preserved.
"""
from __future__ import annotations

import struct
import statistics

import gsloc_rewrite as gx


REGION_FIELD = 3
REGION_DEVICE_FIELD = 2
DEVICE_LOCATION_FIELD = 6
LOCATION_LAT_FIELD = 1
LOCATION_LON_FIELD = 2
COORDINATE_SCALE = 10_000_000


def _coordinate_bytes(degrees):
    scaled = gx.js_round(float(degrees) * COORDINATE_SCALE)
    if not -(1 << 31) <= scaled < (1 << 31):
        raise ValueError("WifiTile coordinate outside sfixed32 range: %r" % degrees)
    return struct.pack("<i", scaled)


def _location_values(fields):
    lat = lon = None
    for field in fields:
        if field.wire_type != gx.WIRE_I32:
            continue
        value = struct.unpack("<i", field.value)[0] / COORDINATE_SCALE
        if field.field_no == LOCATION_LAT_FIELD:
            lat = value
        elif field.field_no == LOCATION_LON_FIELD:
            lon = value
    return lat, lon


def _rewrite_location(payload, transform):
    fields = gx.parse_fields(payload)
    lat, lon = _location_values(fields)
    if lat is None or lon is None:
        return payload, False
    if not gx._valid_coordinate(lat, lon):
        # sfixed32*1e7 physically permits +/-214 deg, so a tile can carry an
        # out-of-range or (-180,-180) no-fix marker. Such a point is excluded
        # from the anchor (translate_wifi_tile line filtering the WGS84 box) and
        # must not be translated: feeding it to translate_coordinate raises
        # "outside valid range", and collapsing it to a real point would
        # fabricate a fix. Leave the marker bytes untouched, mirroring the
        # gsloc_rewrite was_valid handling.
        return payload, False
    lat, lon = transform(lat, lon)

    out = bytearray()
    for field in fields:
        if field.field_no == LOCATION_LAT_FIELD and field.wire_type == gx.WIRE_I32:
            out += gx.tag(LOCATION_LAT_FIELD, gx.WIRE_I32) + _coordinate_bytes(lat)
        elif field.field_no == LOCATION_LON_FIELD and field.wire_type == gx.WIRE_I32:
            out += gx.tag(LOCATION_LON_FIELD, gx.WIRE_I32) + _coordinate_bytes(lon)
        else:
            out += field.raw
    return bytes(out), True


def _rewrite_device(payload, transform):
    out = bytearray()
    changed = False
    for field in gx.parse_fields(payload):
        if field.field_no == DEVICE_LOCATION_FIELD and field.wire_type == gx.WIRE_LEN:
            replacement, did = _rewrite_location(field.value, transform)
            if did:
                out += gx.len_field(DEVICE_LOCATION_FIELD, replacement)
                changed = True
                continue
        out += field.raw
    return bytes(out), changed


def _rewrite_region(payload, transform):
    out = bytearray()
    count = 0
    for field in gx.parse_fields(payload):
        if field.field_no == REGION_DEVICE_FIELD and field.wire_type == gx.WIRE_LEN:
            replacement, did = _rewrite_device(field.value, transform)
            if did:
                out += gx.len_field(REGION_DEVICE_FIELD, replacement)
                count += 1
                continue
        out += field.raw
    return bytes(out), count


def _rewrite_wifi_tile(payload, transform):
    out = bytearray()
    count = 0
    for field in gx.parse_fields(payload):
        if field.field_no == REGION_FIELD and field.wire_type == gx.WIRE_LEN:
            replacement, changed = _rewrite_region(field.value, transform)
            if changed:
                out += gx.len_field(REGION_FIELD, replacement)
                count += changed
                continue
        out += field.raw
    return bytes(out), count


def rewrite_wifi_tile(payload, lat, lon):
    """Collapse all devices to one point (kept for fixtures/fallback only)."""
    return _rewrite_wifi_tile(payload, lambda _old_lat, _old_lon: (lat, lon))


def translate_wifi_tile(payload, target_lat, target_lon):
    """Translate a tile to the target while retaining its local AP geometry."""
    locations = [
        item for item in decode_locations(payload)
        if -90 <= item[0] <= 90 and -180 <= item[1] <= 180
    ]
    if not locations:
        replacement, count = rewrite_wifi_tile(payload, target_lat, target_lon)
        return replacement, count, None
    anchor = (
        statistics.median(item[0] for item in locations),
        statistics.median(item[1] for item in locations),
    )

    def transform(lat, lon):
        return gx.translate_coordinate(
            lat, lon, anchor[0], anchor[1], target_lat, target_lon
        )

    replacement, count = _rewrite_wifi_tile(payload, transform)
    return replacement, count, anchor


def decode_locations(payload):
    """Return decoded ``(lat, lon)`` pairs for diagnostics and tests."""
    locations = []
    for region in gx.parse_fields(payload):
        if region.field_no != REGION_FIELD or region.wire_type != gx.WIRE_LEN:
            continue
        for device in gx.parse_fields(region.value):
            if device.field_no != REGION_DEVICE_FIELD or device.wire_type != gx.WIRE_LEN:
                continue
            for entry in gx.parse_fields(device.value):
                if entry.field_no != DEVICE_LOCATION_FIELD or entry.wire_type != gx.WIRE_LEN:
                    continue
                lat, lon = _location_values(gx.parse_fields(entry.value))
                if lat is not None and lon is not None:
                    locations.append((lat, lon))
    return locations
