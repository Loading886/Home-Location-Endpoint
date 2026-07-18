#!/usr/bin/env python3
"""Small operator CLI installed as ``hle``."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ETC = Path(os.environ.get("HLE_ETC", "/etc/home-location-endpoint"))
APP = Path(os.environ.get("HLE_APP", "/opt/home-location-endpoint"))
STATE = Path(os.environ.get("HLE_STATE", "/var/lib/home-location-endpoint"))


def run(command, *, check=True):
    return subprocess.run(command, check=check, text=True)


def load_location():
    return json.loads((ETC / "location.json").read_text(encoding="utf-8"))


def command_status(_args):
    location = load_location()
    source = location.get("source", {})
    preset = location["presets"][location["active"]]
    print("Location: %s, %s" % (source.get("city", "unknown"), source.get("country_code", "--")))
    print("Selection: %s" % source.get("selection", "unknown"))
    print("Selected at: %s" % source.get("selected_at", "unknown"))
    print("Jitter: %sm / %ss" % (
        location.get("jitter", {}).get("radius_m", 0),
        location.get("jitter", {}).get("period_s", 0),
    ))
    print("Coordinate stored: %.6f, %.6f" % (preset["lat"], preset["lon"]))
    for service in ("home-location-endpoint.service", "xray.service"):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service], check=False
        )
        print("%s: %s" % (service, "active" if result.returncode == 0 else "inactive"))


def command_relocate(args):
    if os.geteuid() != 0:
        raise SystemExit("hle relocate must run as root")
    command = [
        sys.executable,
        str(APP / "location_picker.py"),
        "--output", str(ETC / "location.json"),
        "--cache", str(STATE / "city-boundary.json"),
        "--fallback-radius-m", str(args.fallback_radius_m),
    ]
    run(command)
    os.chmod(ETC / "location.json", 0o640)
    group = __import__("grp").getgrnam("home-location").gr_gid
    os.chown(ETC / "location.json", 0, group)
    print("The interceptor reloads this point automatically; no restart was needed.")


def command_show_link(_args):
    sys.stdout.write((ETC / "node-uri.txt").read_text(encoding="utf-8"))


def command_profile(_args):
    print(ETC / "Home-Location-Endpoint-CA.mobileconfig")


def command_verify(_args):
    failures = 0
    checks = [
        (["/usr/local/bin/xray", "run", "-test", "-config", "/usr/local/etc/xray/config.json"], "Xray config"),
        ([sys.executable, "-m", "py_compile", str(APP / "interceptor.py")], "interceptor syntax"),
        (["openssl", "verify", "-CAfile", str(ETC / "ca.crt"), str(ETC / "leaf.crt")], "leaf certificate"),
    ]
    for command, label in checks:
        result = subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        okay = result.returncode == 0
        print("%s: %s" % (label, "OK" if okay else "FAIL"))
        failures += 0 if okay else 1
    for service in ("home-location-endpoint.service", "xray.service"):
        result = subprocess.run(["systemctl", "is-active", "--quiet", service], check=False)
        okay = result.returncode == 0
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
    args.func(args)


if __name__ == "__main__":
    main()
