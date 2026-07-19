#!/usr/bin/env python3
"""Render the Xray server config, proxy URI, and iOS CA profile."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import tempfile
import urllib.parse
import uuid as uuid_module
from pathlib import Path


LOCATION_DOMAINS = [
    "full:gs-loc.apple.com",
    "full:gs-loc-cn.apple.com",
    "regexp:^gspe85(-[0-9]+)?(-cn)?-ssl\\.ls\\.apple\\.com$",
]
PROXY_PROTOCOLS = {"vless-reality", "ss2022"}
SS2022_METHOD = "2022-blake3-aes-256-gcm"
SHORT_ID_RE = re.compile(r"^[0-9a-f]{0,16}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temporary, mode)
        handle = os.fdopen(fd, "wb")
        fd = None
        with handle:
            handle.write(data)
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


def validate_port(port: int) -> int:
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def validate_uuid(value: str) -> str:
    parsed = uuid_module.UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("UUID must use canonical form")
    return str(parsed)


def validate_host(value: str, *, allow_ip: bool) -> str:
    value = value.strip().rstrip(".")
    if allow_ip:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    if not HOST_RE.fullmatch(value):
        raise ValueError("invalid hostname: %s" % value)
    return value.lower()


def validate_key(value: str, name: str) -> str:
    value = value.strip()
    if not KEY_RE.fullmatch(value) or len(value) != 43:
        raise ValueError("invalid %s" % name)
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid %s" % name) from exc
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != 32 or canonical != value:
        raise ValueError("invalid %s" % name)
    return value


def validate_short_id(value: str) -> str:
    value = value.strip().lower()
    if not SHORT_ID_RE.fullmatch(value) or len(value) % 2:
        raise ValueError("short ID must contain 0-16 lowercase hex characters, even length")
    return value


def validate_ss2022_password(value: str) -> str:
    value = value.strip()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid SS2022 password") from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("SS2022 password must be canonical base64 for 32 bytes")
    return value


def validate_listen_host(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if not address.is_unspecified:
        raise ValueError("listen address must be 0.0.0.0 or ::")
    return str(address)


def validate_fallback_limit(value: dict | None) -> dict | None:
    if value is None:
        return None
    if set(value) != {"afterBytes", "bytesPerSec", "burstBytesPerSec"}:
        raise ValueError("fallback limit has unexpected fields")
    normalized = {key: int(item) for key, item in value.items()}
    if not 1 <= normalized["afterBytes"] <= 1024 * 1024 * 1024:
        raise ValueError("fallback afterBytes is outside the safe range")
    if not 1 <= normalized["bytesPerSec"] <= 128 * 1024 * 1024:
        raise ValueError("fallback bytesPerSec is outside the safe range")
    if not normalized["bytesPerSec"] <= normalized["burstBytesPerSec"] <= 256 * 1024 * 1024:
        raise ValueError("fallback burstBytesPerSec is outside the safe range")
    return normalized


def split_target(target: str) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0 or target[closing + 1:closing + 2] != ":":
            raise ValueError("invalid bracketed REALITY target")
        host = validate_host(target[1:closing], allow_ip=True)
        port = validate_port(int(target[closing + 2:]))
        return host, port
    host, separator, port_text = target.rpartition(":")
    if not separator or not host:
        raise ValueError("REALITY target must be host:port")
    return validate_host(host, allow_ip=True), validate_port(int(port_text))


def build_xray_config(
    *,
    port: int,
    client_uuid: str | None = None,
    reality_sni: str | None = None,
    reality_target: str | None = None,
    private_key: str | None = None,
    short_id: str | None = None,
    protocol: str = "vless-reality",
    ss_password: str | None = None,
    listen_host: str = "0.0.0.0",
    fallback_upload: dict | None = None,
    fallback_download: dict | None = None,
) -> dict:
    port = validate_port(port)
    if protocol not in PROXY_PROTOCOLS:
        raise ValueError("unsupported proxy protocol: %s" % protocol)
    listen_host = validate_listen_host(listen_host)
    fallback_upload = validate_fallback_limit(fallback_upload)
    fallback_download = validate_fallback_limit(fallback_download)

    if protocol == "vless-reality":
        client_uuid = validate_uuid(client_uuid or "")
        reality_sni = validate_host(reality_sni or "", allow_ip=False)
        target_host, target_port = split_target(reality_target or "")
        private_key = validate_key(private_key or "", "REALITY private key")
        short_id = validate_short_id(short_id or "")
        target_display = (
            "[%s]:%d" % (target_host, target_port)
            if ":" in target_host
            else "%s:%d" % (target_host, target_port)
        )
        reality_settings = {
            "show": False,
            "target": target_display,
            "xver": 0,
            "serverNames": [reality_sni],
            "privateKey": private_key,
            "shortIds": [short_id],
        }
        if fallback_upload is not None:
            reality_settings["limitFallbackUpload"] = fallback_upload
        if fallback_download is not None:
            reality_settings["limitFallbackDownload"] = fallback_download
        stream_settings = {
            "network": "raw",
            "security": "reality",
            "realitySettings": reality_settings,
        }
        inbound_tag = "vless-reality-in"
        inbound = {
            "tag": inbound_tag,
            "listen": listen_host,
            "port": port,
            "protocol": "vless",
            "settings": {
                "clients": [
                    {
                        "id": client_uuid,
                        "email": "home-location-client",
                        "flow": "xtls-rprx-vision",
                    }
                ],
                "decryption": "none",
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": True,
            },
            "streamSettings": stream_settings,
        }
    else:
        if fallback_upload is not None or fallback_download is not None:
            raise ValueError("REALITY fallback limits do not apply to SS2022")
        inbound_tag = "ss2022-in"
        inbound = {
            "tag": inbound_tag,
            "listen": listen_host,
            "port": port,
            "protocol": "shadowsocks",
            "settings": {
                "network": "tcp,udp",
                "method": SS2022_METHOD,
                "password": validate_ss2022_password(ss_password or ""),
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": True,
            },
        }

    if listen_host == "::":
        inbound.setdefault("streamSettings", {
            "network": "raw",
            "security": "none",
        })["sockopt"] = {"v6only": False}

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {"domainStrategy": "UseIP"},
            },
            {
                "tag": "location-interceptor",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "AsIs",
                    "redirect": "127.0.0.1:10451",
                    # Current Xray blocks VLESS-to-private targets by default.
                    # This exception is deliberately limited to our one local TCP service.
                    "finalRules": [
                        {
                            "action": "allow",
                            "network": "tcp",
                            "ip": ["127.0.0.1/32"],
                            "port": "10451",
                        }
                    ],
                },
            },
            {
                "tag": "block-location-quic",
                "protocol": "blackhole",
                "settings": {},
            },
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": [inbound_tag],
                    "network": "udp",
                    "port": "443",
                    "domain": list(LOCATION_DOMAINS),
                    "outboundTag": "block-location-quic",
                },
                {
                    "type": "field",
                    "inboundTag": [inbound_tag],
                    "network": "tcp",
                    "port": "443",
                    "domain": list(LOCATION_DOMAINS),
                    "outboundTag": "location-interceptor",
                }
            ],
        },
    }


def _uri_host(server: str) -> str:
    server = validate_host(server, allow_ip=True)
    return "[%s]" % server if ":" in server else server


def build_vless_uri(
    *,
    server: str,
    port: int,
    client_uuid: str,
    reality_sni: str,
    public_key: str,
    short_id: str,
) -> str:
    params = [
        ("encryption", "none"),
        ("flow", "xtls-rprx-vision"),
        ("security", "reality"),
        ("sni", validate_host(reality_sni, allow_ip=False)),
        ("fp", "chrome"),
        ("pbk", validate_key(public_key, "REALITY public key")),
        ("sid", validate_short_id(short_id)),
        ("type", "tcp"),
        ("headerType", "none"),
        ("packetEncoding", "xudp"),
    ]
    return "vless://%s@%s:%d?%s#%s" % (
        validate_uuid(client_uuid),
        _uri_host(server),
        validate_port(port),
        urllib.parse.urlencode(params),
        urllib.parse.quote("Home-Location-Endpoint"),
    )


def build_ss2022_uri(*, server: str, port: int, password: str) -> str:
    user_info = "%s:%s" % (SS2022_METHOD, validate_ss2022_password(password))
    encoded = base64.urlsafe_b64encode(user_info.encode("ascii")).decode("ascii")
    encoded = encoded.rstrip("=")
    return "ss://%s@%s:%d#%s" % (
        encoded,
        _uri_host(server),
        validate_port(port),
        urllib.parse.quote("Home-Location-Endpoint"),
    )


def build_ca_profile(ca_der: bytes) -> bytes:
    if not ca_der:
        raise ValueError("CA certificate is empty")
    fingerprint = hashlib.sha256(ca_der).hexdigest()
    root_id = str(
        uuid_module.uuid5(
            uuid_module.NAMESPACE_URL,
            "https://github.com/Loading886/Home-Location-Endpoint/ca/" + fingerprint,
        )
    ).upper()
    profile_id = str(
        uuid_module.uuid5(
            uuid_module.NAMESPACE_URL,
            "https://github.com/Loading886/Home-Location-Endpoint/profile/" + fingerprint,
        )
    ).upper()
    payload = {
        "PayloadContent": [
            {
                "PayloadCertificateFileName": "Home-Location-Endpoint-CA.cer",
                "PayloadContent": ca_der,
                "PayloadDescription": "Trusts only the locally generated location endpoint CA.",
                "PayloadDisplayName": "Home Location Endpoint CA",
                "PayloadIdentifier": "org.loading886.home-location-endpoint.ca.%s" % root_id.lower(),
                "PayloadType": "com.apple.security.root",
                "PayloadUUID": root_id,
                "PayloadVersion": 1,
            }
        ],
        "PayloadDescription": (
            "Installs the private CA used to rewrite scoped Apple network-location responses."
        ),
        "PayloadDisplayName": "Home Location Endpoint CA",
        "PayloadIdentifier": "org.loading886.home-location-endpoint.%s" % profile_id.lower(),
        "PayloadOrganization": "Home Location Endpoint",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": profile_id,
        "PayloadVersion": 1,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--uri", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--ca-der", type=Path)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--protocol", choices=sorted(PROXY_PROTOCOLS), default="vless-reality")
    parser.add_argument("--uuid")
    parser.add_argument("--reality-sni")
    parser.add_argument("--reality-target")
    parser.add_argument("--private-key")
    parser.add_argument("--public-key")
    parser.add_argument("--short-id")
    parser.add_argument("--ss-password")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--fallback-upload-after", type=int)
    parser.add_argument("--fallback-upload-rate", type=int)
    parser.add_argument("--fallback-upload-burst", type=int)
    parser.add_argument("--fallback-download-after", type=int)
    parser.add_argument("--fallback-download-rate", type=int)
    parser.add_argument("--fallback-download-burst", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.profile) != bool(args.ca_der):
        raise SystemExit("--profile and --ca-der must be supplied together")
    upload_values = (
        args.fallback_upload_after,
        args.fallback_upload_rate,
        args.fallback_upload_burst,
    )
    download_values = (
        args.fallback_download_after,
        args.fallback_download_rate,
        args.fallback_download_burst,
    )
    if any(value is not None for value in upload_values + download_values) and not all(
        value is not None for value in upload_values + download_values
    ):
        raise SystemExit("all fallback rate-limit arguments must be supplied together")
    fallback_upload = None
    fallback_download = None
    if all(value is not None for value in upload_values):
        fallback_upload = dict(zip(
            ("afterBytes", "bytesPerSec", "burstBytesPerSec"), upload_values
        ))
        fallback_download = dict(zip(
            ("afterBytes", "bytesPerSec", "burstBytesPerSec"), download_values
        ))
    config = build_xray_config(
        port=args.port,
        protocol=args.protocol,
        client_uuid=args.uuid,
        reality_sni=args.reality_sni,
        reality_target=args.reality_target,
        private_key=args.private_key,
        short_id=args.short_id,
        ss_password=args.ss_password,
        listen_host=args.listen,
        fallback_upload=fallback_upload,
        fallback_download=fallback_download,
    )
    if args.protocol == "vless-reality":
        uri = build_vless_uri(
            server=args.server,
            port=args.port,
            client_uuid=args.uuid or "",
            reality_sni=args.reality_sni or "",
            public_key=args.public_key or "",
            short_id=args.short_id or "",
        )
    else:
        uri = build_ss2022_uri(
            server=args.server,
            port=args.port,
            password=args.ss_password or "",
        )
    atomic_write(
        args.config,
        (json.dumps(config, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        0o640,
    )
    atomic_write(args.uri, (uri + "\n").encode("utf-8"), 0o600)
    if args.profile:
        atomic_write(args.profile, build_ca_profile(args.ca_der.read_bytes()), 0o644)
    print("rendered Xray config and node URI%s" % (
        " with iOS CA profile" if args.profile else ""
    ))


if __name__ == "__main__":
    main()
