#!/usr/bin/env python3
"""Validate that a candidate is suitable as a REALITY camouflage target."""

from __future__ import annotations

import argparse
import socket
import ssl

from home_location_endpoint.render import split_target, validate_host


def probe_reality_target(sni: str, target: str, *, timeout: float = 10.0) -> dict:
    """Require a valid certificate, TLS 1.3, and negotiated HTTP/2."""
    sni = validate_host(sni, allow_ip=False)
    host, port = split_target(target)
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["h2"])
    with socket.create_connection((host, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=sni) as tls_socket:
            version = tls_socket.version()
            alpn = tls_socket.selected_alpn_protocol()
    if version != "TLSv1.3":
        raise ValueError("target did not negotiate TLS 1.3")
    if alpn != "h2":
        raise ValueError("target did not negotiate HTTP/2 through ALPN")
    return {"sni": sni, "host": host, "port": port, "tls": version, "alpn": alpn}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sni")
    parser.add_argument("target")
    args = parser.parse_args()
    result = probe_reality_target(args.sni, args.target)
    print("{sni} -> {host}:{port} {tls} {alpn}".format(**result))


if __name__ == "__main__":
    main()
