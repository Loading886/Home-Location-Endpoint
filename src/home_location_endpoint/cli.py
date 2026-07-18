#!/usr/bin/env python3
"""Small operator CLI installed as ``hle``."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ETC = Path(os.environ.get("HLE_ETC", "/etc/home-location-endpoint"))
APP = Path(os.environ.get("HLE_APP", "/opt/home-location-endpoint"))
STATE = Path(os.environ.get("HLE_STATE", "/var/lib/home-location-endpoint"))
LOG = Path(os.environ.get("HLE_LOG_DIR", "/var/log/home-location-endpoint"))
LOCK = Path(os.environ.get("HLE_LOCK", "/run/home-location-endpoint.lock"))
XRAY_CONFIG_DIR = Path(os.environ.get("HLE_XRAY_CONFIG_DIR", "/usr/local/etc/xray"))
XRAY_BIN = Path("/usr/local/bin/xray")
HLE_SYMLINK = Path("/usr/local/sbin/hle")
SYSTEMD_DIR = Path("/etc/systemd/system")
LOGROTATE_FILE = Path("/etc/logrotate.d/home-location-endpoint")
SYSCTL_FILE = Path("/etc/sysctl.d/99-home-location-endpoint.conf")
PROFILE_NAME = "Home-Location-Endpoint-CA.mobileconfig"
PROFILE_PORT = 18080
PROFILE_TIMEOUT_MINUTES = 100
PROFILE_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


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
    path = ETC / PROFILE_NAME
    if not path.is_file():
        raise SystemExit("CA profile is missing: %s" % path)
    print(path)


class _IPv6HTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET6


def _validate_profile_host(value):
    value = value.strip().rstrip(".")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    if not PROFILE_HOST_RE.fullmatch(value):
        raise ValueError("invalid hostname: %s" % value)
    return value.lower()


def _profile_download_host(explicit_host):
    host = explicit_host or _install_inventory().get("HLE_SERVER", "")
    host = host.strip().strip("[]")
    if not host:
        raise SystemExit(
            "no client-reachable address is recorded; use --host <address>"
        )
    host = _validate_profile_host(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.is_unspecified:
        raise SystemExit("--host must be an address clients can reach")
    return address.compressed


def _profile_bind_address(value):
    try:
        return ipaddress.ip_address(value.strip().strip("[]")).compressed
    except ValueError as exc:
        raise SystemExit("--bind must be an IPv4 or IPv6 address") from exc


def _profile_url_host(host):
    try:
        return "[%s]" % ipaddress.IPv6Address(host).compressed
    except ipaddress.AddressValueError:
        return host


def _profile_fingerprint(ca_der):
    digest = hashlib.sha256(ca_der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def command_profile_serve(args):
    path = ETC / PROFILE_NAME
    if not path.is_file():
        raise SystemExit("CA profile is missing: %s" % path)
    if not profile_matches_ca():
        raise SystemExit("CA profile does not match the installed CA")
    if not 0 <= args.port <= 65535:
        raise SystemExit("--port must be between 0 and 65535")
    if not 1 <= args.timeout_minutes <= 1440:
        raise SystemExit("--timeout-minutes must be between 1 and 1440")

    host = _profile_download_host(args.host)
    try:
        host_is_ipv6 = isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        host_is_ipv6 = False
    bind = _profile_bind_address(
        args.bind or ("::" if host_is_ipv6 else "0.0.0.0")
    )
    profile_bytes = path.read_bytes()
    ca_der = (ETC / "ca.der").read_bytes()
    token = secrets.token_urlsafe(24)
    download_path = "/%s/%s" % (token, PROFILE_NAME)
    state = {"downloaded": False}

    class ProfileHandler(http.server.BaseHTTPRequestHandler):
        server_version = "Home-Location-Endpoint"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, _format, *_arguments):
            return

        def _headers(self, status):
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 200:
                self.send_header(
                    "Content-Type", "application/x-apple-aspen-config"
                )
                self.send_header(
                    "Content-Disposition", 'attachment; filename="%s"' % PROFILE_NAME
                )
                self.send_header("Content-Length", str(len(profile_bytes)))
            else:
                self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self._headers(200 if self.path == download_path else 404)

        def do_GET(self):
            if self.path != download_path or state["downloaded"]:
                self._headers(404)
                return
            self._headers(200)
            try:
                self.wfile.write(profile_bytes)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                return
            state["downloaded"] = True

    server_class = _IPv6HTTPServer if ":" in bind else http.server.HTTPServer
    with server_class((bind, args.port), ProfileHandler) as server:
        port = server.server_address[1]
        url = "http://%s:%d%s" % (_profile_url_host(host), port, download_path)
        print("Temporary CA profile download / 临时 CA 描述文件下载")
        print("URL / 下载地址: %s" % url)
        print("Valid for / 有效时间: %d minutes / 分钟" % args.timeout_minutes)
        print("Downloads / 下载次数: 1")
        print("CA SHA-256 / CA 指纹: %s" % _profile_fingerprint(ca_der))
        print(
            "Security / 安全提示: this is temporary HTTP; verify the fingerprint "
            "before trusting the CA. / 这是临时 HTTP，请在信任 CA 前核对指纹。"
        )
        print(
            "Firewall / 防火墙: TCP %d must temporarily reach this host. "
            "/ 请临时确保 TCP %d 可以到达本机。" % (port, port)
        )
        if not args.no_qr and sys.stdout.isatty() and shutil.which("qrencode"):
            print("Scan with iPhone Camera / 使用 iPhone 相机扫码:")
            subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
        print("Waiting for one successful download... / 正在等待一次成功下载……")

        deadline = time.monotonic() + args.timeout_minutes * 60
        try:
            while not state["downloaded"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                server.timeout = min(1.0, remaining)
                server.handle_request()
        except KeyboardInterrupt:
            print("Download server stopped. / 下载服务已停止。")
            return

    if state["downloaded"]:
        print("Profile downloaded; server closed. / 描述文件已下载，服务已关闭。")
    else:
        print("Download link expired; server closed. / 下载链接已过期，服务已关闭。")


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


def _systemctl(*arguments):
    try:
        result = subprocess.run(
            ["systemctl", *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 127
    return result.returncode


def _remove_path(path):
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            print("warning: unsupported managed path type: %s" % path)
            return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        print("warning: could not remove %s: %s" % (path, exc))
        return False
    return True


def _delete_user(name):
    try:
        import pwd

        pwd.getpwnam(name)
    except KeyError:
        return True
    except ImportError:
        return False
    try:
        result = subprocess.run(
            ["userdel", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _delete_group(name):
    try:
        import grp

        grp.getgrnam(name)
    except KeyError:
        return True
    except ImportError:
        return False
    try:
        result = subprocess.run(
            ["groupdel", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _install_inventory():
    values = {}
    try:
        for line in (ETC / "install.env").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.startswith("HLE_"):
                values[key] = value.strip().strip("'\"")
    except OSError:
        return {}
    return values


def _inventory_flag(inventory, name):
    return inventory.get(name) == "1"


def _valid_installer_marker():
    marker = ETC / "managed-by-installer"
    try:
        metadata = marker.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        not marker.is_symlink()
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
    )


def command_uninstall(args):
    if os.geteuid() != 0:
        raise SystemExit("hle uninstall must run as root")
    if not shutil.which("systemctl"):
        raise SystemExit("hle uninstall requires systemctl")
    if not _valid_installer_marker():
        raise SystemExit(
            "refusing to uninstall without a root-owned Home-Location-Endpoint marker"
        )
    # install_mode() reports "full" only when this host actually has the managed
    # Xray node (node-uri.txt / recorded mode); a modifier-only host that merely
    # runs the operator's own Xray reports "modifier-only", so full teardown
    # never touches a proxy core this project did not install.
    mode = install_mode()
    full = mode == "full"
    if not args.yes:
        print("This permanently removes Home-Location-Endpoint from this host:")
        print("  - stops and deletes the location interceptor service%s"
              % (" and the managed Xray service" if full else ""))
        print("  - deletes %s, %s, %s, and %s" % (ETC, APP, STATE, LOG))
        if full:
            print("  - deletes the managed Xray binary, its config, and the TCP sysctl file")
        print("  - removes the scoped CA files on this host")
        print("  - removes only low-privilege accounts recorded as installer-created")
        if not full:
            print("  - leaves your own proxy core, ports, and firewall untouched")
        print("It does NOT delete the CA profile already installed on your iPhone.")
        try:
            answer = input("Type 'yes' to continue: ").strip()
        except EOFError:
            answer = ""
        if answer != "yes":
            raise SystemExit("uninstall aborted")
    inventory = {}
    port = None
    failures = []
    preserved_accounts = []
    with operation_lock():
        # Re-check after acquiring the installer lock. The confirmation prompt
        # may have been open while another process completed an upgrade.
        if not _valid_installer_marker() or install_mode() != mode:
            raise SystemExit("installation state changed; rerun hle uninstall")
        inventory = _install_inventory()
        port_value = inventory.get("HLE_PORT", "") if full else ""
        port = port_value if port_value.isdigit() else None
        _systemctl("disable", "--now", "home-location-endpoint.service")
        if full:
            _systemctl("disable", "--now", "xray.service")
        services = ["home-location-endpoint.service"]
        if full:
            services.append("xray.service")
        still_active = [
            service for service in services
            if _systemctl("is-active", "--quiet", service) == 0
        ]
        if still_active:
            raise SystemExit(
                "refusing to remove files while services remain active: %s"
                % ", ".join(still_active)
            )

        managed_paths = [
            SYSTEMD_DIR / "home-location-endpoint.service",
            LOGROTATE_FILE,
            STATE,
            LOG,
        ]
        if full:
            managed_paths.extend([
                SYSTEMD_DIR / "xray.service",
                SYSCTL_FILE,
                XRAY_CONFIG_DIR,
                XRAY_BIN,
            ])
        for path in managed_paths:
            if not _remove_path(path):
                failures.append(str(path))
        if _systemctl("daemon-reload") != 0:
            failures.append("systemctl daemon-reload")

        account_inventory = [
            ("home-location", "HLE_CREATED_HOME_USER", "HLE_CREATED_HOME_GROUP")
        ]
        if full:
            account_inventory.append(
                ("xray", "HLE_CREATED_XRAY_USER", "HLE_CREATED_XRAY_GROUP")
            )
        for name, user_flag, group_flag in account_inventory:
            remove_user = _inventory_flag(inventory, user_flag)
            remove_group = _inventory_flag(inventory, group_flag)
            group_was_created = remove_group
            if remove_user and not _delete_user(name):
                failures.append("user %s" % name)
                remove_group = False
            elif not remove_user:
                preserved_accounts.append("user %s" % name)
            if remove_group and not _delete_group(name):
                failures.append("group %s" % name)
            elif not group_was_created:
                preserved_accounts.append("group %s" % name)

        # Preserve the CLI, marker, and inventory when an earlier step failed so
        # the operator can inspect the state and retry the same safe command.
        if not failures:
            for path in (ETC, APP, HLE_SYMLINK):
                if not _remove_path(path):
                    failures.append(str(path))

    if failures:
        print("Home-Location-Endpoint uninstall is incomplete.")
        for failure in failures:
            print("  - not removed: %s" % failure)
        raise SystemExit(1)
    print("Home-Location-Endpoint managed files removed.")
    if preserved_accounts:
        print("Preserved accounts not recorded as installer-created: %s."
              % ", ".join(preserved_accounts))
    print(
        "Reminder: delete the CA profile from the iPhone "
        "(Settings > General > VPN & Device Management) and remove the client node%s."
        % (" / VLESS URI" if full else " and the location routing you added")
    )
    if full:
        print("TCP sysctl tuning stays live until the next reboot.")
        if port and shutil.which("ufw"):
            print(
                "Firewall safety: review any TCP %s UFW rule manually; "
                "the uninstaller does not delete a rule it cannot prove it created."
                % port
            )


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
    profile = subparsers.add_parser(
        "profile", help="show or temporarily serve the CA profile"
    )
    profile.set_defaults(func=command_profile)
    profile_actions = profile.add_subparsers(dest="profile_action")
    profile_serve = profile_actions.add_parser(
        "serve", help="serve a one-download temporary profile URL"
    )
    profile_serve.add_argument(
        "--host", help="client-reachable address shown in the download URL"
    )
    profile_serve.add_argument(
        "--bind", help="local IPv4/IPv6 bind address (default follows --host)"
    )
    profile_serve.add_argument(
        "--port", type=int, default=PROFILE_PORT,
        help="temporary HTTP port; 0 chooses a random free port",
    )
    profile_serve.add_argument(
        "--timeout-minutes", type=int, default=PROFILE_TIMEOUT_MINUTES,
        help="link lifetime in minutes (default: 100)",
    )
    profile_serve.add_argument(
        "--no-qr", action="store_true", help="do not print a terminal QR code"
    )
    profile_serve.set_defaults(func=command_profile_serve)
    verify = subparsers.add_parser("verify", help="run local integrity checks")
    verify.set_defaults(func=command_verify)
    uninstall = subparsers.add_parser(
        "uninstall", help="stop services and remove managed files and scoped CA"
    )
    uninstall.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    uninstall.set_defaults(func=command_uninstall)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("hle: %s" % exc) from exc


if __name__ == "__main__":
    main()
