#!/usr/bin/env python3
"""Dependency-free Apple WLOC codec and geometry-preserving translator.

This implementation handles only the protobuf wire fields required by Apple's
network-location response. Unknown fields are preserved byte-for-byte.

Observed wire format:

  wifi response body = <10-byte opaque header> || BlockBSSIDApple(protobuf)

  message BlockBSSIDApple {          // top level
    optional int64  unknown0  = 1;
    repeated WifiDetected wifi = 2;
    optional int32  unknown1  = 3;
    optional int32  unknown2  = 4;
    optional string api_name  = 5;
  }
  message WifiDetected { required string bssid = 1; optional Location location = 2; }
  message Location {
    optional int64 latitude  = 1;    // degrees * 1e8
    optional int64 longitude = 2;    // degrees * 1e8
    optional int64 accuracy  = 3;    // meters
    // ... fields 4..12, 21 unknown, preserved byte-for-byte
  }

"Unknown location" is the sentinel latitude == longitude == -18000000000
(i.e. -180.00000000), which is a negative int64 -> a 10-byte two's-complement
varint. Sentinel-only batches are passed through unchanged. Valid batches are
translated around a new center while retaining their internal geometry.

The value written into the response is WGS84, which is also the coordinate frame
returned by the installer's IP/city providers.

Fields that are not rewritten are re-emitted from their original raw bytes, so
the output differs from the input only in the coordinate/accuracy varints and the
recomputed length prefixes of the containers above them -- nothing else moves.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import statistics
import sys
import time

# ---------------------------------------------------------------------------
# protobuf wire codec (varint / tag / field split / serialize)
# ---------------------------------------------------------------------------

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_I32 = 5

_MASK64 = (1 << 64) - 1


def read_varint(buf, pos):
    """Return (unsigned_value, new_pos). Accepts up to a 10-byte int64 varint."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def write_varint(value):
    """Encode a NON-negative integer as an unsigned base-128 varint."""
    if value < 0:
        raise ValueError("write_varint expects an unsigned value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_int64(value):
    """protobuf int64: two's complement in 64 bits, then varint (negatives -> 10 bytes)."""
    return write_varint(value & _MASK64)


def decode_int64(unsigned):
    """Reinterpret a raw unsigned varint as a signed int64."""
    unsigned &= _MASK64
    return unsigned - (1 << 64) if unsigned & (1 << 63) else unsigned


def tag(field_no, wire_type):
    return write_varint((field_no << 3) | wire_type)


class Field:
    """One parsed protobuf field with its original bytes for lossless passthrough."""

    __slots__ = ("field_no", "wire_type", "value", "raw")

    def __init__(self, field_no, wire_type, value, raw):
        self.field_no = field_no      # int
        self.wire_type = wire_type    # int
        self.value = value            # varint: unsigned int; len/i64/i32: bytes
        self.raw = raw                # exact tag+value bytes as seen on the wire


def parse_fields(buf):
    """Split a protobuf message into a flat list of Field objects (non-recursive)."""
    pos = 0
    out = []
    n = len(buf)
    while pos < n:
        start = pos
        key, pos = read_varint(buf, pos)
        field_no = key >> 3
        wire = key & 0x07
        if field_no == 0:
            raise ValueError("invalid protobuf field number 0")
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == WIRE_I64:
            if pos + 8 > n:
                raise ValueError("truncated 64-bit field")
            value = bytes(buf[pos:pos + 8])
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            value = bytes(buf[pos:pos + length])
            pos += length
        elif wire == WIRE_I32:
            if pos + 4 > n:
                raise ValueError("truncated 32-bit field")
            value = bytes(buf[pos:pos + 4])
            pos += 4
        else:
            raise ValueError("unsupported wire type %d" % wire)
        out.append(Field(field_no, wire, value, bytes(buf[start:pos])))
    return out


def len_field(field_no, payload):
    """Encode a length-delimited field (tag + length + payload)."""
    return tag(field_no, WIRE_LEN) + write_varint(len(payload)) + payload


def js_round(value):
    """Round halves toward positive infinity, matching the observed encoder."""
    return math.floor(value + 0.5)


def _scaled(deg):
    return encode_int64(js_round(deg * 1e8))


# ---------------------------------------------------------------------------
# gs-loc response framing + rewrite
# ---------------------------------------------------------------------------

RESP_HEADER_LEN = 10  # wifi response: r.content[10:] is the BlockBSSIDApple protobuf
SENTINEL_SCALED = -18000000000  # -180.00000000: locationd "no fix" marker
NO_FIX_SOURCE = "no-fix-passthrough"

# The 10-byte response header (confirmed against the live server 2026-07-18) is:
#   [0:2]  version marker      (0x00 0x01)
#   [2:6]  opaque marker       (0x00000001 in observed captures; left untouched)
#   [6:10] big-endian uint32   = length of the BlockBSSIDApple protobuf that follows
# A rewrite can change the block length (the sentinel/accuracy varints shrink), so
# the length field must be recomputed or locationd reads a truncated/overlong body.
HEADER_LEN_LO = 6
HEADER_LEN_HI = 10


def split_response(body):
    """Return (10-byte header, protobuf block). Raises if the body is too short."""
    if len(body) < RESP_HEADER_LEN:
        raise ValueError("gs-loc body shorter than the 10-byte header")
    return bytes(body[:RESP_HEADER_LEN]), bytes(body[RESP_HEADER_LEN:])


def header_block_length(header):
    """The block length the header declares (big-endian uint32 at bytes [6:10])."""
    return int.from_bytes(header[HEADER_LEN_LO:HEADER_LEN_HI], "big")


def _with_block_length(header, block_len):
    out = bytearray(header)
    out[HEADER_LEN_LO:HEADER_LEN_HI] = block_len.to_bytes(4, "big")
    return bytes(out)


def _rewrite_location(loc_bytes, lat, lon, accuracy):
    """Rewrite one Location payload. Returns (new_bytes, changed)."""
    fields = parse_fields(loc_bytes)
    has_lat = any(f.field_no == 1 and f.wire_type == WIRE_VARINT for f in fields)
    has_lon = any(f.field_no == 2 and f.wire_type == WIRE_VARINT for f in fields)
    if not (has_lat and has_lon):
        return loc_bytes, False
    out = bytearray()
    for f in fields:
        if f.field_no == 1 and f.wire_type == WIRE_VARINT:
            out += tag(1, WIRE_VARINT) + _scaled(lat)
        elif f.field_no == 2 and f.wire_type == WIRE_VARINT:
            out += tag(2, WIRE_VARINT) + _scaled(lon)
        elif f.field_no == 3 and f.wire_type == WIRE_VARINT and accuracy is not None:
            out += tag(3, WIRE_VARINT) + encode_int64(int(accuracy))
        else:
            out += f.raw
    return bytes(out), True


# Top-level block dispatch: a wifi entry is field 2
# (WifiDetected, location at field 2); a cell entry is field 22 or 24
# (CellResponse, location at field 5). locationd uses cell positioning whenever
# Wi-Fi is off, so both must be rewritten or a Wi-Fi spoof conflicts with the
# real cell fix and locationd rejects both.
WIFI_ENTRY_FIELD = 2
WIFI_LOCATION_FIELD = 2
CELL_ENTRY_FIELDS = (22, 24)
CELL_LOCATION_FIELD = 5


def _rewrite_entry(entry_bytes, location_field, lat, lon, accuracy):
    """Rewrite the Location submessage carried at `location_field` of one entry."""
    out = bytearray()
    changed = False
    for f in parse_fields(entry_bytes):
        if f.field_no == location_field and f.wire_type == WIRE_LEN:
            new_loc, did = _rewrite_location(f.value, lat, lon, accuracy)
            if did:
                out += len_field(location_field, new_loc)
                changed = True
                continue
        out += f.raw
    return bytes(out), changed


def rewrite_block(block_bytes, lat, lon, accuracy=None):
    """Rewrite every wifi (field 2) and cell (field 22/24) Location to (lat, lon).

    Returns (bytes, count) where count is the number of entries rewritten across
    both families. A block that is a wifi response has no cell entries and vice
    versa, so the single pass handles either response type.
    """
    out = bytearray()
    count = 0
    for f in parse_fields(block_bytes):
        location_field = None
        entry_accuracy = accuracy
        if f.wire_type == WIRE_LEN and f.field_no == WIFI_ENTRY_FIELD:
            location_field = WIFI_LOCATION_FIELD
        elif f.wire_type == WIRE_LEN and f.field_no in CELL_ENTRY_FIELDS:
            location_field = CELL_LOCATION_FIELD
            # A cell Location's field 3 is the tower-fix accuracy/confidence
            # (kilometre-scale). Overwriting it with a wifi-style 25 m makes the
            # cell fix implausible and locationd rejects it, so preserve it.
            entry_accuracy = None
        if location_field is not None:
            new_entry, did = _rewrite_entry(f.value, location_field, lat, lon, entry_accuracy)
            if did:
                out += len_field(f.field_no, new_entry)
                count += 1
                continue
        out += f.raw
    return bytes(out), count


def rewrite_response(body, lat, lon, accuracy=None, fix_length=True):
    """Rewrite a full gs-loc response body (wifi or cell). lat/lon are WGS84. Returns (bytes, count).

    With fix_length (default), the header's big-endian block-length field is
    recomputed. To avoid corrupting a body whose header does not match the
    assumed framing, this asserts the declared length equals the actual block
    length before touching it -- a mismatch raises rather than silently rewrites.
    """
    header, block = split_response(body)
    new_block, count = rewrite_block(block, lat, lon, accuracy)
    if fix_length:
        declared = header_block_length(header)
        if declared != len(block):
            raise ValueError(
                "header block-length %d != actual %d; framing assumption violated"
                % (declared, len(block))
            )
        header = _with_block_length(header, len(new_block))
    return header + new_block, count


# ---------------------------------------------------------------------------
# decode (inspection / tests)
# ---------------------------------------------------------------------------

def _decode_location(loc_bytes):
    lat = lon = acc = None
    for f in parse_fields(loc_bytes):
        if f.wire_type != WIRE_VARINT:
            continue
        if f.field_no == 1:
            lat = decode_int64(f.value) / 1e8
        elif f.field_no == 2:
            lon = decode_int64(f.value) / 1e8
        elif f.field_no == 3:
            acc = decode_int64(f.value)
    return lat, lon, acc


def decode_block(block_bytes):
    """Return a list of {kind, bssid, lat, lon, accuracy, has_location} per entry.

    Decodes both wifi entries (field 2, bssid + location@2) and cell entries
    (field 22/24, location@5) so tests can inspect either response type.
    """
    entries = []
    for f in parse_fields(block_bytes):
        if f.wire_type != WIRE_LEN:
            continue
        if f.field_no == WIFI_ENTRY_FIELD:
            kind, location_field = "wifi", WIFI_LOCATION_FIELD
        elif f.field_no in CELL_ENTRY_FIELDS:
            kind, location_field = "cell", CELL_LOCATION_FIELD
        else:
            continue
        bssid = None
        cell_values = {}
        lat = lon = acc = None
        has_location = False
        for sub in parse_fields(f.value):
            if kind == "wifi" and sub.field_no == 1 and sub.wire_type == WIRE_LEN:
                bssid = sub.value.decode("latin1")
            elif kind == "cell" and sub.field_no in (1, 2, 3, 4) and sub.wire_type == WIRE_VARINT:
                cell_values[sub.field_no] = int(sub.value)
            elif sub.field_no == location_field and sub.wire_type == WIRE_LEN:
                has_location = True
                lat, lon, acc = _decode_location(sub.value)
        entries.append({
            "kind": kind,
            "bssid": bssid,
            "cell_key": tuple(cell_values.get(i) for i in (1, 2, 3, 4)),
            "lat": lat,
            "lon": lon,
            "accuracy": acc,
            "has_location": has_location,
        })
    return entries


def decode_response(body):
    _header, block = split_response(body)
    return decode_block(block)


# ---------------------------------------------------------------------------
# Geometry-preserving translation for live iOS location-assist batches
# ---------------------------------------------------------------------------

REQUEST_CELL_FIELDS = (25, 29)


def _read_pascal_string(buf, pos):
    if pos + 2 > len(buf):
        raise ValueError("truncated ARPC Pascal-string length")
    length = int.from_bytes(buf[pos:pos + 2], "big")
    pos += 2
    if pos + length > len(buf):
        raise ValueError("truncated ARPC Pascal string")
    return buf[pos:pos + length].decode("latin1"), pos + length


def parse_request_context(body):
    """Decode the small ARPC envelope used by ``POST /clls/wloc``.

    Returns the request function, an optional coordinate anchor, requested Wi-Fi
    BSSIDs, and requested cell identities. iOS 26 uses function 3 with request
    payload fields 1/2 as lat/lon * 1e8 for 400-entry location-assist batches.
    """
    if len(body) < 2:
        raise ValueError("ARPC request too short")
    pos = 2  # version
    strings = []
    for _ in range(3):
        value, pos = _read_pascal_string(body, pos)
        strings.append(value)
    if pos + 8 > len(body):
        raise ValueError("truncated ARPC request header")
    function_id = int.from_bytes(body[pos:pos + 4], "big")
    payload_len = int.from_bytes(body[pos + 4:pos + 8], "big")
    pos += 8
    if pos + payload_len > len(body):
        raise ValueError("truncated ARPC request payload")
    fields = parse_fields(body[pos:pos + payload_len])

    context = {
        "function_id": function_id,
        "locale": strings[0],
        "anchor": None,
        "wifi_bssids": set(),
        "cell_keys": set(),
    }
    if function_id == 3:
        scalars = {
            field.field_no: decode_int64(field.value)
            for field in fields
            if field.field_no in (1, 2) and field.wire_type == WIRE_VARINT
        }
        if 1 in scalars and 2 in scalars:
            candidate = (scalars[1] / 1e8, scalars[2] / 1e8)
            if _valid_coordinate(*candidate):
                context["anchor"] = candidate

    for field in fields:
        if field.field_no == 2 and field.wire_type == WIRE_LEN:
            for sub in parse_fields(field.value):
                if sub.field_no == 1 and sub.wire_type == WIRE_LEN:
                    context["wifi_bssids"].add(sub.value.decode("latin1"))
        elif field.field_no in REQUEST_CELL_FIELDS and field.wire_type == WIRE_LEN:
            values = {}
            for sub in parse_fields(field.value):
                if sub.field_no in (1, 2, 3, 4) and sub.wire_type == WIRE_VARINT:
                    values[sub.field_no] = int(sub.value)
            key = tuple(values.get(i) for i in (1, 2, 3, 4))
            if all(value is not None for value in key):
                context["cell_keys"].add(key)
    return context


def _valid_coordinate(lat, lon):
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (lat == -180 and lon == -180)
    )


def _coordinate_median(entries):
    coordinates = [
        (entry["lat"], entry["lon"])
        for entry in entries
        if _valid_coordinate(entry["lat"], entry["lon"])
    ]
    if not coordinates:
        return None
    return (
        statistics.median(item[0] for item in coordinates),
        statistics.median(item[1] for item in coordinates),
    )


def _is_proven_no_fix(entries, block):
    """True only when a response is known to contain no usable coordinates.

    Apple's explicit no-fix sentinel is (-180, -180). Passing such a response
    through cannot disclose a real coordinate and avoids manufacturing a
    degenerate batch in which every AP/tower is assigned the same target. An
    empty protobuf block is also safe. Anything malformed or merely unknown is
    deliberately not classified as no-fix, so the interceptor can fail closed.
    """
    if not block:
        return True
    locations = [entry for entry in entries if entry["has_location"]]
    return bool(locations) and all(
        entry["lat"] == -180.0 and entry["lon"] == -180.0
        for entry in locations
    )


def resolve_translation_anchor(entries, request_context=None):
    """Pick the strongest source anchor and return ``((lat, lon), source)``."""
    request_context = request_context or {}
    wifi_bssids = request_context.get("wifi_bssids", set())
    cell_keys = request_context.get("cell_keys", set())
    matched = [
        entry for entry in entries
        if (
            (entry["kind"] == "wifi" and entry["bssid"] in wifi_bssids)
            or (entry["kind"] == "cell" and entry["cell_key"] in cell_keys)
        )
    ]
    anchor = _coordinate_median(matched)
    if anchor is not None:
        return anchor, "requested-identity"
    if request_context.get("anchor") is not None:
        return request_context["anchor"], "request-coordinate"
    anchor = _coordinate_median(entries)
    if anchor is not None:
        return anchor, "response-median"
    return None, "none"


def translate_coordinate(lat, lon, source_lat, source_lon, target_lat, target_lon):
    """Translate a point while preserving its local north/east displacement."""
    new_lat = target_lat + (lat - source_lat)
    source_scale = math.cos(math.radians(source_lat))
    target_scale = math.cos(math.radians(target_lat))
    if abs(target_scale) < 1e-9:
        target_scale = 1e-9
    new_lon = target_lon + (lon - source_lon) * source_scale / target_scale
    if not (-90 <= new_lat <= 90 and -180 <= new_lon <= 180):
        raise ValueError("translated coordinate outside valid range")
    return new_lat, new_lon


def _translate_location(loc_bytes, source, target, accuracy, preserve_accuracy):
    fields = parse_fields(loc_bytes)
    lat, lon, _old_accuracy = _decode_location(loc_bytes)
    if lat is None or lon is None:
        return loc_bytes, False
    was_valid = _valid_coordinate(lat, lon)
    if was_valid:
        new_lat, new_lon = translate_coordinate(
            lat, lon, source[0], source[1], target[0], target[1]
        )
    else:
        new_lat, new_lon = target

    out = bytearray()
    for field in fields:
        if field.field_no == 1 and field.wire_type == WIRE_VARINT:
            out += tag(1, WIRE_VARINT) + _scaled(new_lat)
        elif field.field_no == 2 and field.wire_type == WIRE_VARINT:
            out += tag(2, WIRE_VARINT) + _scaled(new_lon)
        elif (
            field.field_no == 3
            and field.wire_type == WIRE_VARINT
            and not preserve_accuracy
            and not was_valid
            and accuracy is not None
        ):
            out += tag(3, WIRE_VARINT) + encode_int64(int(accuracy))
        else:
            out += field.raw
    return bytes(out), True


def translate_block(block_bytes, source, target, accuracy=None):
    """Translate Wi-Fi/cell response geometry instead of collapsing every point."""
    out = bytearray()
    count = 0
    for field in parse_fields(block_bytes):
        if field.wire_type != WIRE_LEN:
            out += field.raw
            continue
        if field.field_no == WIFI_ENTRY_FIELD:
            location_field = WIFI_LOCATION_FIELD
        elif field.field_no in CELL_ENTRY_FIELDS:
            location_field = CELL_LOCATION_FIELD
        else:
            out += field.raw
            continue

        entry_out = bytearray()
        changed = False
        for sub in parse_fields(field.value):
            if sub.field_no == location_field and sub.wire_type == WIRE_LEN:
                replacement, did = _translate_location(
                    sub.value,
                    source,
                    target,
                    accuracy,
                    preserve_accuracy=(field.field_no in CELL_ENTRY_FIELDS),
                )
                if did:
                    entry_out += len_field(location_field, replacement)
                    changed = True
                    continue
            entry_out += sub.raw
        if changed:
            out += len_field(field.field_no, bytes(entry_out))
            count += 1
        else:
            out += field.raw
    return bytes(out), count


def translate_response(body, target_lat, target_lon, request_body=None, accuracy=None):
    """Translate a full WLOC response and return ``(body, count, anchor, source)``."""
    header, block = split_response(body)
    declared = header_block_length(header)
    if declared != len(block):
        raise ValueError(
            "header block-length %d != actual %d; framing assumption violated"
            % (declared, len(block))
        )
    context = None
    if request_body:
        try:
            context = parse_request_context(request_body)
        except ValueError:
            context = None
    entries = decode_block(block)
    anchor, source = resolve_translation_anchor(entries, context)
    if anchor is None:
        if _is_proven_no_fix(entries, block):
            return body, 0, None, NO_FIX_SOURCE
        raise ValueError("WLOC response has no safe translation anchor")
    new_block, count = translate_block(
        block, anchor, (target_lat, target_lon), accuracy=accuracy
    )
    return _with_block_length(header, len(new_block)) + new_block, count, anchor, source


# ---------------------------------------------------------------------------
# static coordinate presets loaded by the interceptor
# ---------------------------------------------------------------------------

def load_presets(path):
    """Load a gsloc-presets.json config."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_target(presets, key=None):
    """Return ``(lat, lon, accuracy)`` from a validated WGS84 preset."""
    if key is None:
        key = presets["active"]
    entry = presets["presets"][key]
    lat = float(entry["lat"])
    lon = float(entry["lon"])
    if entry.get("datum", "wgs84").lower() != "wgs84":
        raise ValueError("only WGS84 targets are supported")
    accuracy = entry.get("accuracy_m", presets.get("default_accuracy_m", 25))
    return lat, lon, accuracy


# ---------------------------------------------------------------------------
# deterministic smooth micro-drift around the selected target
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6371008.8
JITTER_RADIUS_MAX_M = 100.0
JITTER_PERIOD_MIN_S = 30.0
JITTER_PERIOD_MAX_S = 3600.0


def _finite_number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError("%s must be a number" % name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % name) from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError("%s must be between %s and %s" % (name, low, high))
    return number


def _jitter_config(config, name, base):
    if config is None:
        return dict(base)
    if not isinstance(config, dict):
        raise ValueError("%s must be an object" % name)
    unknown = set(config) - {"enabled", "radius_m", "period_s"}
    if unknown:
        raise ValueError("%s has unknown keys: %s" % (name, ", ".join(sorted(unknown))))
    out = dict(base)
    if "enabled" in config:
        if not isinstance(config["enabled"], bool):
            raise ValueError("%s.enabled must be a boolean" % name)
        out["enabled"] = config["enabled"]
    if "radius_m" in config:
        out["radius_m"] = _finite_number(
            config["radius_m"], "%s.radius_m" % name, 0.0, JITTER_RADIUS_MAX_M
        )
    if "period_s" in config:
        out["period_s"] = _finite_number(
            config["period_s"], "%s.period_s" % name,
            JITTER_PERIOD_MIN_S, JITTER_PERIOD_MAX_S,
        )
    return out


def resolve_jitter(presets, key=None):
    """Return ``(radius_m, period_s)`` after global + per-preset overrides.

    Existing files without a ``jitter`` object retain the historical static
    behavior. A preset can override any global field with its own ``jitter``
    object; setting ``enabled`` false disables drift only for that preset.
    """
    if key is None:
        key = presets["active"]
    base = {"enabled": False, "radius_m": 0.0, "period_s": 120.0}
    global_config = _jitter_config(presets.get("jitter"), "jitter", base)
    entry = presets["presets"][key]
    resolved = _jitter_config(entry.get("jitter"), "%s.jitter" % key, global_config)
    radius = resolved["radius_m"] if resolved["enabled"] else 0.0
    return radius, resolved["period_s"]


def _jitter_offset(seed, trajectory_key, bucket, radius_m):
    message = (
        "home-location-endpoint-jitter-v1\0%s\0%d" % (trajectory_key, bucket)
    ).encode("utf-8")
    digest = hmac.new(seed, message, hashlib.sha256).digest()
    scale = float(1 << 64)
    radial_u = (int.from_bytes(digest[:8], "big") + 0.5) / scale
    angle_u = (int.from_bytes(digest[8:16], "big") + 0.5) / scale
    distance = radius_m * math.sqrt(radial_u)
    angle = 2.0 * math.pi * angle_u
    return distance * math.cos(angle), distance * math.sin(angle)


def offset_coordinate(lat, lon, north_m, east_m):
    """Move a WGS84 point by a local north/east offset on a sphere."""
    lat = _finite_number(lat, "latitude", -90.0, 90.0)
    lon = _finite_number(lon, "longitude", -180.0, 180.0)
    north_m = _finite_number(north_m, "north_m", -JITTER_RADIUS_MAX_M, JITTER_RADIUS_MAX_M)
    east_m = _finite_number(east_m, "east_m", -JITTER_RADIUS_MAX_M, JITTER_RADIUS_MAX_M)
    distance = math.hypot(north_m, east_m)
    if distance == 0:
        return lat, lon

    angular_distance = distance / EARTH_RADIUS_M
    bearing = math.atan2(east_m, north_m)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    sin_lat2 = (
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), ((math.degrees(lon2) + 180.0) % 360.0) - 180.0


def smooth_jitter_target(lat, lon, radius_m, period_s, seed, trajectory_key,
                         timestamp=None):
    """Return a smooth, deterministic point within ``radius_m`` of ``lat/lon``.

    Each period gets a seed-derived point uniformly distributed in the disk.
    Smoothstep interpolation joins adjacent points with zero velocity at each
    boundary. The disk is convex, so every interpolated point remains inside
    the configured radius. No timer or mutable runtime state is required.
    """
    radius_m = _finite_number(radius_m, "radius_m", 0.0, JITTER_RADIUS_MAX_M)
    period_s = _finite_number(
        period_s, "period_s", JITTER_PERIOD_MIN_S, JITTER_PERIOD_MAX_S
    )
    if radius_m == 0:
        return float(lat), float(lon)
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < 16:
        raise ValueError("jitter seed must contain at least 16 bytes")
    if not isinstance(trajectory_key, str) or not trajectory_key:
        raise ValueError("trajectory_key must be a non-empty string")
    if timestamp is None:
        timestamp = time.time()
    timestamp = _finite_number(timestamp, "timestamp", -1e12, 1e12)

    bucket = math.floor(timestamp / period_s)
    phase = (timestamp - bucket * period_s) / period_s
    smooth = phase * phase * (3.0 - 2.0 * phase)
    north0, east0 = _jitter_offset(bytes(seed), trajectory_key, bucket, radius_m)
    north1, east1 = _jitter_offset(bytes(seed), trajectory_key, bucket + 1, radius_m)
    north = north0 + (north1 - north0) * smooth
    east = east0 + (east1 - east0) * smooth
    return offset_coordinate(lat, lon, north, east)


# ---------------------------------------------------------------------------
# synthetic builders for tests; not on the production path
# ---------------------------------------------------------------------------

def build_location(lat, lon, accuracy=None, extra=()):
    payload = tag(1, WIRE_VARINT) + _scaled(lat)
    payload += tag(2, WIRE_VARINT) + _scaled(lon)
    if accuracy is not None:
        payload += tag(3, WIRE_VARINT) + encode_int64(int(accuracy))
    for field_no, value in extra:
        payload += tag(field_no, WIRE_VARINT) + encode_int64(int(value))
    return payload


def build_sentinel_location(accuracy=None):
    payload = tag(1, WIRE_VARINT) + encode_int64(SENTINEL_SCALED)
    payload += tag(2, WIRE_VARINT) + encode_int64(SENTINEL_SCALED)
    if accuracy is not None:
        payload += tag(3, WIRE_VARINT) + encode_int64(int(accuracy))
    return payload


def build_wifi(bssid, location_payload=None):
    raw = bssid.encode("latin1") if isinstance(bssid, str) else bytes(bssid)
    out = len_field(1, raw)
    if location_payload is not None:
        out += len_field(2, location_payload)
    return out


def build_block(wifis, unknown0=0, api_name=None):
    out = tag(1, WIRE_VARINT) + encode_int64(int(unknown0))
    for wifi in wifis:
        out += len_field(2, wifi)
    if api_name is not None:
        out += len_field(5, api_name.encode("latin1"))
    return out


def build_cell(location_payload=None, mcc=460, mnc=0, cid=1, lac=1):
    """Build a CellResponse: MCC(1)/MNC(2)/CID(3)/LAC(4) scalars + location@5.

    MCC/MNC are varints at fields 1/2 -- the exact fields a Location uses -- so
    this doubles as a regression fixture proving the rewriter never mistakes a
    cell's MCC/MNC for coordinates.
    """
    out = tag(1, WIRE_VARINT) + encode_int64(int(mcc))
    out += tag(2, WIRE_VARINT) + encode_int64(int(mnc))
    out += tag(3, WIRE_VARINT) + encode_int64(int(cid))
    out += tag(4, WIRE_VARINT) + encode_int64(int(lac))
    if location_payload is not None:
        out += len_field(CELL_LOCATION_FIELD, location_payload)
    return out


def build_cell_response(cells, marker=b"\x00\x01\x00\x00\x00\x01", entry_field=22):
    """Build a full cell response (cells at top-level field 22 or 24)."""
    if entry_field not in CELL_ENTRY_FIELDS:
        raise ValueError("cell entry field must be one of %r" % (CELL_ENTRY_FIELDS,))
    block = bytearray()
    for cell in cells:
        block += len_field(entry_field, cell)
    return bytes(marker) + len(block).to_bytes(4, "big") + bytes(block)


def build_response(wifis, marker=b"\x00\x01\x00\x00\x00\x01", unknown0=0, api_name=None):
    """Build a full wifi response with a live-shaped header (6-byte marker + BE length)."""
    if len(marker) != HEADER_LEN_LO:
        raise ValueError("marker must be exactly %d bytes" % HEADER_LEN_LO)
    block = build_block(wifis, unknown0=unknown0, api_name=api_name)
    return bytes(marker) + len(block).to_bytes(4, "big") + block


# ---------------------------------------------------------------------------
# manual decode CLI for an operator-supplied response body
# ---------------------------------------------------------------------------

def _main(argv):
    if len(argv) != 2:
        sys.exit("usage: gsloc_rewrite.py <response-body.bin>")
    with open(argv[1], "rb") as handle:
        body = handle.read()
    header, block = split_response(body)
    print("header(%d)=%s" % (len(header), header.hex()))
    for i, entry in enumerate(decode_block(block)):
        print("wifi[%d] bssid=%s lat=%s lon=%s acc=%s"
              % (i, entry["bssid"], entry["lat"], entry["lon"], entry["accuracy"]))


if __name__ == "__main__":
    _main(sys.argv)
