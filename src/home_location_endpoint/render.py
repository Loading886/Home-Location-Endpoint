#!/usr/bin/env python3
"""Render the Xray server config, VLESS URI, and iOS CA profile."""

from __future__ import annotations

import argparse
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
    if not KEY_RE.fullmatch(value):
        raise ValueError("invalid %s" % name)
    return value


def validate_short_id(value: str) -> str:
    value = value.strip().lower()
    if not SHORT_ID_RE.fullmatch(value) or len(value) % 2:
        raise ValueError("short ID must contain 0-16 lowercase hex characters, even length")
    return value


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
    client_uuid: str,
    reality_sni: str,
    reality_target: str,
    private_key: str,
    short_id: str,
) -> dict:
    port = validate_port(port)
    client_uuid = validate_uuid(client_uuid)
    reality_sni = validate_host(reality_sni, allow_ip=False)
    target_host, target_port = split_target(reality_target)
    private_key = validate_key(private_key, "REALITY private key")
    short_id = validate_short_id(short_id)
    target_display = (
        "[%s]:%d" % (target_host, target_port)
        if ":" in target_host
        else "%s:%d" % (target_host, target_port)
    )

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "vless-reality-in",
                "listen": "0.0.0.0",
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
                "streamSettings": {
                    "network": "raw",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": target_display,
                        "xver": 0,
                        "serverNames": [reality_sni],
                        "privateKey": private_key,
                        "shortIds": [short_id],
                    },
                },
            }
        ],
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
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["vless-reality-in"],
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


def build_ca_profile(ca_der: bytes) -> bytes:
    if not ca_der:
        raise ValueError("CA certificate is empty")
    root_id = str(uuid_module.uuid4()).upper()
    profile_id = str(uuid_module.uuid4()).upper()
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
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--ca-der", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--reality-sni", required=True)
    parser.add_argument("--reality-target", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--short-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_xray_config(
        port=args.port,
        client_uuid=args.uuid,
        reality_sni=args.reality_sni,
        reality_target=args.reality_target,
        private_key=args.private_key,
        short_id=args.short_id,
    )
    uri = build_vless_uri(
        server=args.server,
        port=args.port,
        client_uuid=args.uuid,
        reality_sni=args.reality_sni,
        public_key=args.public_key,
        short_id=args.short_id,
    )
    atomic_write(
        args.config,
        (json.dumps(config, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        0o640,
    )
    atomic_write(args.uri, (uri + "\n").encode("utf-8"), 0o600)
    atomic_write(args.profile, build_ca_profile(args.ca_der.read_bytes()), 0o644)
    print("rendered Xray config, node URI, and iOS CA profile")


if __name__ == "__main__":
    main()
