#!/usr/bin/env python3
"""Small operator CLI installed as ``hle``."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


ETC = Path(os.environ.get("HLE_ETC", "/etc/home-location-endpoint"))
APP = Path(os.environ.get("HLE_APP", "/opt/home-location-endpoint"))
STATE = Path(os.environ.get("HLE_STATE", "/var/lib/home-location-endpoint"))
LOCK = Path(os.environ.get("HLE_LOCK", "/run/home-location-endpoint.lock"))


def run(command, *, check=True):
    return subprocess.run(command, check=check, text=True)


def load_location():
    return json.loads((ETC / "location.json").read_text(encoding="utf-8"))


def install_mode():
    path = ETC / "mode"
    if path.exists():
        mode = path.read_text(encoding="utf-8").strip()
        if mode in {"full", "modifier-only"}:
            return mode
        raise SystemExit("invalid installation mode recorded in %s" % path)
    return "full" if (ETC / "node-uri.txt").exists() else "modifier-only"


@contextmanager
def operation_lock():
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - installed CLI runs on Linux
        raise SystemExit("operation locking requires Linux fcntl") from exc
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="ascii") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another Home-Location-Endpoint operation is running") from exc
        yield


def command_status(_args):
    location = load_location()
    mode = install_mode()
    source = location.get("source", {})
    preset = location["presets"][location["active"]]
    print("Mode: %s" % mode)
    print("Location: %s, %s" % (source.get("city", "unknown"), source.get("country_code", "--")))
    print("Selection: %s" % source.get("selection", "unknown"))
    print("Selected at: %s" % source.get("selected_at", "unknown"))
    print("Jitter: %sm / %ss" % (
        location.get("jitter", {}).get("radius_m", 0),
        location.get("jitter", {}).get("period_s", 0),
    ))
    print("Coordinate stored: %.6f, %.6f" % (preset["lat"], preset["lon"]))
    services = ["home-location-endpoint.service"]
    if mode == "full":
        services.append("xray.service")
    for service in services:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service], check=False
        )
        print("%s: %s" % (service, "active" if result.returncode == 0 else "inactive"))


def command_relocate(args):
    if os.geteuid() != 0:
        raise SystemExit("hle relocate must run as root")
    try:
        group = __import__("grp").getgrnam("home-location").gr_gid
    except KeyError as exc:
        raise SystemExit("required group home-location is missing") from exc
    command = [
        sys.executable,
        str(APP / "location_picker.py"),
        "--output", str(ETC / "location.json"),
        "--cache", str(STATE / "city-boundary.json"),
        "--fallback-radius-m", str(args.fallback_radius_m),
        "--output-mode", "0640",
        "--output-uid", "0",
        "--output-gid", str(group),
    ]
    with operation_lock():
        run(command)
    print("The interceptor reloads this point automatically; no restart was needed.")


def command_show_link(_args):
    path = ETC / "node-uri.txt"
    if not path.exists():
        raise SystemExit("no node URI: this host uses modifier-only mode")
    sys.stdout.write(path.read_text(encoding="utf-8"))


def command_profile(_args):
    path = ETC / "Home-Location-Endpoint-CA.mobileconfig"
    if not path.is_file():
        raise SystemExit("CA profile is missing: %s" % path)
    print(path)


def location_is_valid():
    location = load_location()
    active = location["active"]
    preset = location["presets"][active]
    lat = float(preset["lat"])
    lon = float(preset["lon"])
    return (
        preset.get("datum") == "wgs84"
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def profile_matches_ca():
    profile = plistlib.loads(
        (ETC / "Home-Location-Endpoint-CA.mobileconfig").read_bytes()
    )
    payloads = profile.get("PayloadContent", [])
    return (
        profile.get("PayloadType") == "Configuration"
        and isinstance(payloads, list)
        and len(payloads) == 1
        and isinstance(payloads[0], dict)
        and payloads[0].get("PayloadType") == "com.apple.security.root"
        and payloads[0].get("PayloadContent") == (ETC / "ca.der").read_bytes()
    )


def certificate_key_matches():
    cert_public = subprocess.run(
        ["openssl", "x509", "-in", str(ETC / "leaf.crt"), "-pubkey", "-noout"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    cert_der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        check=True,
        input=cert_public.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    key_der = subprocess.run(
        [
            "openssl", "pkey", "-in", str(ETC / "leaf.key"),
            "-pubout", "-outform", "DER",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return cert_der == key_der


def loopback_interceptor_is_listening():
    try:
        lines = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return False
    expected_port = "%04X" % 10451
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        address, port = fields[1].split(":", 1)
        if address.upper() == "0100007F" and port.upper() == expected_port:
            return fields[3].upper() == "0A"
    return False


def managed_permissions_are_safe(mode):
    try:
        import grp

        home_gid = grp.getgrnam("home-location").gr_gid
    except (ImportError, KeyError):
        return False
    checks = [
        (ETC, 0, home_gid, 0o750),
        (ETC / "mode", 0, 0, 0o644),
        (ETC / "install.env", 0, 0, 0o600),
        (ETC / "managed-by-installer", 0, 0, 0o600),
        (ETC / "location.json", 0, home_gid, 0o640),
        (ETC / "jitter.seed", 0, home_gid, 0o640),
        (ETC / "leaf.crt", 0, home_gid, 0o640),
        (ETC / "leaf.key", 0, home_gid, 0o640),
        (ETC / "ca.crt", 0, 0, 0o644),
        (ETC / "ca.der", 0, 0, 0o644),
        (ETC / "Home-Location-Endpoint-CA.mobileconfig", 0, 0, 0o644),
    ]
    if mode == "full":
        checks.append((ETC / "node-uri.txt", 0, 0, 0o600))
    for path, uid, gid, expected_mode in checks:
        try:
            metadata = path.stat(follow_symlinks=False)
        except (FileNotFoundError, OSError):
            return False
        if (
            path.is_symlink()
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            return False
    return True


def command_verify(_args):
    mode = install_mode()
    failures = 0
    checks = [
        ([
            sys.executable,
            "-c",
            "compile(open(%r, encoding='utf-8').read(), %r, 'exec')"
            % (str(APP / "interceptor.py"), str(APP / "interceptor.py")),
        ], "interceptor syntax"),
        (["openssl", "verify", "-CAfile", str(ETC / "ca.crt"), str(ETC / "leaf.crt")], "leaf certificate"),
        (["openssl", "x509", "-checkend", "2592000", "-noout", "-in", str(ETC / "ca.crt")], "CA validity >=30d"),
        (["openssl", "x509", "-checkend", "2592000", "-noout", "-in", str(ETC / "leaf.crt")], "leaf validity >=30d"),
        (["openssl", "x509", "-checkhost", "gs-loc.apple.com", "-noout", "-in", str(ETC / "leaf.crt")], "leaf hostname scope"),
        (["openssl", "x509", "-checkhost", "gs-loc-cn.apple.com", "-noout", "-in", str(ETC / "leaf.crt")], "leaf CN hostname scope"),
        (["openssl", "x509", "-checkhost", "gspe85-9-cn-ssl.ls.apple.com", "-noout", "-in", str(ETC / "leaf.crt")], "leaf assist hostname scope"),
    ]
    if mode == "full":
        checks.insert(0, (
            ["/usr/local/bin/xray", "run", "-test", "-config", "/usr/local/etc/xray/config.json"],
            "Xray config",
        ))
    else:
        checks.append((
            [sys.executable, "-m", "json.tool", str(ETC / "xray-location-routing.example.json")],
            "Xray integration example",
        ))
    for command, label in checks:
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            okay = result.returncode == 0
        except OSError:
            okay = False
        print("%s: %s" % (label, "OK" if okay else "FAIL"))
        failures += 0 if okay else 1
    function_checks = [
        (location_is_valid, "location config"),
        (profile_matches_ca, "iOS profile matches CA"),
        (certificate_key_matches, "leaf key pair"),
        (lambda: not (ETC / "ca.key").exists(), "CA private key removed"),
        (loopback_interceptor_is_listening, "loopback interceptor"),
        (lambda: managed_permissions_are_safe(mode), "managed file permissions"),
    ]
    for check, label in function_checks:
        try:
            okay = bool(check())
        except Exception:
            okay = False
        print("%s: %s" % (label, "OK" if okay else "FAIL"))
        failures += 0 if okay else 1
    services = ["home-location-endpoint.service"]
    if mode == "full":
        services.append("xray.service")
    for service in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", service], check=False
            )
            okay = result.returncode == 0
        except OSError:
            okay = False
        print("%s: %s" % (service, "OK" if okay else "FAIL"))
        failures += 0 if okay else 1
    raise SystemExit(1 if failures else 0)


def parse_args():
    parser = argparse.ArgumentParser(prog="hle")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show services and selected city")
    status.set_defaults(func=command_status)
    relocate = subparsers.add_parser("relocate", help="choose another random point in the IP city")
    relocate.add_argument("--fallback-radius-m", type=float, default=3000)
    relocate.set_defaults(func=command_relocate)
    show_link = subparsers.add_parser("show-link", help="print the VLESS URI")
    show_link.set_defaults(func=command_show_link)
    profile = subparsers.add_parser("profile", help="print the CA profile path")
    profile.set_defaults(func=command_profile)
    verify = subparsers.add_parser("verify", help="run local integrity checks")
    verify.set_defaults(func=command_verify)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("hle: %s" % exc) from exc


if __name__ == "__main__":
    main()
