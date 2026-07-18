#!/usr/bin/env python3
"""Scoped Apple network-location interceptor for Home Location Endpoint.

Xray terminates VLESS/REALITY, sniffs only the supported Apple location hosts,
and redirects their raw inner TLS stream to this loopback listener. This daemon:

  1. terminates that TLS with a leaf the phone trusts,
     offering only HTTP/1.1;
  2. reads the phone's `POST /clls/wloc` request;
  3. re-originates the request to the REAL Apple gs-loc server (forcing
     Accept-Encoding: identity so the protobuf comes back uncompressed);
  4. rewrites the coordinates in the response to the active static preset via
     gsloc_rewrite (recomputing the header block-length field);
  5. returns the rewritten response to the phone.

The scoped Apple location-assist host family (`gspe85*-ssl.ls.apple.com`) is
also diverted here so its WifiTile response can be translated. This public
build deliberately contains no raw request/response capture facility.
"""
import datetime
import gzip
import io
import os
import re
import socket
import ssl
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import gsloc_rewrite as gx  # noqa: E402
import wifitile_rewrite as wx  # noqa: E402

LISTEN_HOST = os.environ.get("GSLOC_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GSLOC_LISTEN_PORT", "10451"))
LEAF_CRT = os.environ.get("GSLOC_LEAF_CRT", "/etc/home-location-endpoint/leaf.crt")
LEAF_KEY = os.environ.get("GSLOC_LEAF_KEY", "/etc/home-location-endpoint/leaf.key")
PRESETS = os.environ.get("GSLOC_PRESETS", "/etc/home-location-endpoint/location.json")
JITTER_SEED = os.environ.get("GSLOC_JITTER_SEED", "/etc/home-location-endpoint/jitter.seed")
LOG = os.environ.get("GSLOC_LOG", "/var/log/home-location-endpoint/interceptor.log")
UPSTREAM_TIMEOUT = float(os.environ.get("GSLOC_UPSTREAM_TIMEOUT", "10"))
UPSTREAM_ATTEMPTS = max(1, int(os.environ.get("GSLOC_UPSTREAM_ATTEMPTS", "2")))
UPSTREAM_RETRY_DELAY = max(0.0, float(os.environ.get("GSLOC_UPSTREAM_RETRY_DELAY", "0.15")))
CLIENT_TIMEOUT = float(os.environ.get("GSLOC_CLIENT_TIMEOUT", "15"))
UPSTREAM_INSECURE = os.environ.get("GSLOC_UPSTREAM_INSECURE", "0") == "1"
FAIL_CLOSED = os.environ.get("GSLOC_FAIL_CLOSED", "1") != "0"
MAX_REQUEST_BODY = max(1024, int(os.environ.get("GSLOC_MAX_REQUEST_BODY", str(8 * 1024 * 1024))))
MAX_RESPONSE_BODY = max(1024, int(os.environ.get("GSLOC_MAX_RESPONSE_BODY", str(32 * 1024 * 1024))))
MAX_DECOMPRESSED_BODY = max(
    1024, int(os.environ.get("GSLOC_MAX_DECOMPRESSED_BODY", str(64 * 1024 * 1024)))
)
MAX_WORKERS = max(1, int(os.environ.get("GSLOC_MAX_WORKERS", "64")))

# Only these hosts get their /clls/wloc body rewritten; both CN and non-CN so a
# successful spoof that flips the device off the -cn endpoint stays covered.
GSLOC_HOSTS = {"gs-loc.apple.com", "gs-loc-cn.apple.com"}
ASSIST_HOST_RE = re.compile(
    r"^gspe85(?:-[0-9]+)?(?:-cn)?-ssl\.ls\.apple\.com$", re.IGNORECASE
)
WIFI_TILE_PATH = "/wifi_request_tile"
WIFI_TILE_GLOBAL_HOST = "gspe85-ssl.ls.apple.com"

HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "content-length",
    "expect",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_log_lock = threading.Lock()
_presets_cache = {"mtime": None, "value": None}
_presets_lock = threading.Lock()
_jitter_seed_cache = {"fingerprint": None, "value": None}
_jitter_seed_lock = threading.Lock()
_worker_slots = threading.BoundedSemaphore(MAX_WORKERS)


def log(msg):
    line = "%s %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg)
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        try:
            fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def load_jitter_seed():
    """Load the root-managed seed and reload it only when the file changes."""
    try:
        stat_result = os.stat(JITTER_SEED)
    except OSError as exc:
        raise RuntimeError("jitter seed unreadable: %r" % (exc,))
    fingerprint = (stat_result.st_mtime_ns, stat_result.st_size)
    with _jitter_seed_lock:
        if _jitter_seed_cache["fingerprint"] != fingerprint:
            try:
                with open(JITTER_SEED, "rb") as handle:
                    seed = handle.read(65)
            except OSError as exc:
                raise RuntimeError("jitter seed unreadable: %r" % (exc,))
            if not 16 <= len(seed) <= 64:
                raise RuntimeError("jitter seed must contain 16-64 bytes")
            _jitter_seed_cache["value"] = seed
            _jitter_seed_cache["fingerprint"] = fingerprint
        return _jitter_seed_cache["value"]


def active_target(now=None):
    """Return the active target plus its deterministic smooth micro-drift."""
    with _presets_lock:
        try:
            stat_result = os.stat(PRESETS)
        except OSError as exc:
            raise RuntimeError("presets unreadable: %r" % (exc,))
        fingerprint = (stat_result.st_mtime_ns, stat_result.st_size)
        if _presets_cache["mtime"] != fingerprint:
            _presets_cache["value"] = gx.load_presets(PRESETS)
            _presets_cache["mtime"] = fingerprint
        presets = _presets_cache["value"]
    key = presets["active"]
    lat, lon, accuracy = gx.resolve_target(presets, key)
    radius_m, period_s = gx.resolve_jitter(presets, key)
    if radius_m > 0:
        lat, lon = gx.smooth_jitter_target(
            lat, lon, radius_m, period_s, load_jitter_seed(), key,
            timestamp=time.time() if now is None else now,
        )
    return lat, lon, accuracy


def is_assist_host(host):
    return ASSIST_HOST_RE.fullmatch(host or "") is not None


def is_allowed_host(host):
    return host in GSLOC_HOSTS or is_assist_host(host)


def is_wifi_tile_request(host, method, path):
    clean_path = path.split("?", 1)[0]
    return is_assist_host(host) and method == "GET" and clean_path == WIFI_TILE_PATH


def select_origin_host(requested_host, method, path):
    """Use Apple's global WifiTile data when the CN endpoint has no tile.

    iOS may select ``gspe85-cn`` from its CN GeoServices manifest, but observed
    tile keys return 404 there while the identical key exists on ``gspe85``.
    This substitution is deliberately limited to the one location-assist path.
    """
    if is_wifi_tile_request(requested_host, method, path):
        if requested_host.lower() == "gspe85-cn-ssl.ls.apple.com":
            return WIFI_TILE_GLOBAL_HOST
    return requested_host


def operational_path(path):
    """Keep routine logs useful without retaining URL query material."""
    return str(path or "").split("?", 1)[0]


def rewrite_wifi_tile_response(body, content_encoding, lat, lon):
    """Translate a WifiTile body while preserving encoding and AP geometry."""
    encoding = (content_encoding or "").strip().lower()
    if encoding in {"", "identity"}:
        plain = body
    elif encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as archive:
            plain = archive.read(MAX_DECOMPRESSED_BODY + 1)
        if len(plain) > MAX_DECOMPRESSED_BODY:
            raise ValueError("WifiTile decompressed body too large")
    else:
        raise ValueError("unsupported WifiTile content encoding: %s" % encoding)

    replacement, count, anchor = wx.translate_wifi_tile(plain, lat, lon)
    if count == 0:
        if FAIL_CLOSED and plain:
            raise ValueError("WifiTile response has no recognized devices")
        return body, 0, anchor
    if encoding == "gzip":
        replacement = gzip.compress(replacement, mtime=0)
    return replacement, count, anchor


# --- minimal HTTP/1.1 helpers ------------------------------------------------

def read_headers(reader):
    """Read start line + headers. Returns (start_line:str, header_lines:list[str])."""
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = reader.read(1)
        if not chunk:
            raise ConnectionError("EOF before end of headers")
        raw += chunk
        if len(raw) > 65536:
            raise ValueError("header block too large")
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin1")
    lines = head.split("\r\n")
    return lines[0], lines[1:]


def header_map(lines):
    out = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip().lower()] = value.strip()
    return out


def read_body(reader, headers, max_bytes=MAX_REQUEST_BODY):
    te = headers.get("transfer-encoding", "").lower()
    if "chunked" in te:
        body = bytearray()
        while True:
            size_line = b""
            while not size_line.endswith(b"\r\n"):
                b1 = reader.read(1)
                if not b1:
                    raise ConnectionError("EOF in chunk size")
                size_line += b1
                if len(size_line) > 128:
                    raise ValueError("chunk-size line too large")
            size = int(size_line.strip().split(b";", 1)[0], 16)
            if size < 0:
                raise ValueError("negative chunk size")
            if size == 0:
                reader.read(2)  # trailing CRLF after the last chunk
                break
            if len(body) + size > max_bytes:
                raise ValueError("HTTP body too large")
            body += _read_exact(reader, size)
            reader.read(2)  # CRLF after chunk data
        return bytes(body)
    if "content-length" in headers:
        size = int(headers["content-length"])
        if size < 0 or size > max_bytes:
            raise ValueError("HTTP body too large")
        return _read_exact(reader, size)
    return b""  # no body (typical for a request); responses use CL or chunked


def _read_exact(reader, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            raise ConnectionError("EOF: wanted %d, got %d" % (n, len(buf)))
        buf += chunk
    return bytes(buf)


def read_response_body(reader, headers):
    """Response bodies may also be delimited by connection close."""
    te = headers.get("transfer-encoding", "").lower()
    if "chunked" in te or "content-length" in headers:
        return read_body(reader, headers, max_bytes=MAX_RESPONSE_BODY)
    body = reader.read(MAX_RESPONSE_BODY + 1)  # until EOF, but never unbounded
    if len(body) > MAX_RESPONSE_BODY:
        raise ValueError("HTTP response body too large")
    return body


# --- upstream fetch ----------------------------------------------------------

def build_upstream_request(host, request_line, header_lines, body):
    """Rebuild a de-chunked HTTP/1.1 request without dropping Apple X-* headers."""
    forwarded = [("Host", host)]
    for line in header_lines:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() in HOP_BY_HOP_REQUEST_HEADERS:
            continue
        if name.strip().lower() == "accept-encoding":
            continue
        forwarded.append((name.strip(), value.strip()))
    # Identity makes protobuf processing deterministic. If an upstream nevertheless
    # returns encoded bytes, build_response preserves Content-Encoding on passthrough.
    forwarded.append(("Accept-Encoding", "identity"))
    forwarded.append(("Content-Length", str(len(body))))
    forwarded.append(("Connection", "close"))
    head = request_line + "\r\n" + "".join("%s: %s\r\n" % kv for kv in forwarded) + "\r\n"
    return head.encode("latin1") + body


def fetch_upstream(host, request_line, header_lines, body):
    ctx = ssl.create_default_context()
    if UPSTREAM_INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])

    request_bytes = build_upstream_request(host, request_line, header_lines, body)
    last_error = None
    for attempt in range(1, UPSTREAM_ATTEMPTS + 1):
        raw = None
        tls = None
        try:
            raw = socket.create_connection((host, 443), timeout=UPSTREAM_TIMEOUT)
            raw.settimeout(UPSTREAM_TIMEOUT)
            tls = ctx.wrap_socket(raw, server_hostname=host)
            tls.sendall(request_bytes)
            reader = tls.makefile("rb")
            status_line, response_header_lines = read_headers(reader)
            headers = header_map(response_header_lines)
            resp_body = read_response_body(reader, headers)
            return status_line, response_header_lines, headers, resp_body
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            last_error = exc
            if attempt < UPSTREAM_ATTEMPTS:
                log("UPSTREAM_RETRY host=%s attempt=%d/%d err=%r"
                    % (host, attempt, UPSTREAM_ATTEMPTS, exc))
                if UPSTREAM_RETRY_DELAY:
                    time.sleep(UPSTREAM_RETRY_DELAY)
        finally:
            _safe_close(tls)
            if tls is None:
                _safe_close(raw)
    raise last_error


def build_response(status_line, header_lines, headers, body, strip_content_encoding=False):
    keep = []
    for line in header_lines:
        if ":" not in line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name in {"content-length", "transfer-encoding", "connection"}:
            continue
        if strip_content_encoding and name == "content-encoding":
            continue
        keep.append(line)
    keep.append("Content-Length: %d" % len(body))
    keep.append("Connection: close")
    head = status_line + "\r\n" + "\r\n".join(keep) + "\r\n\r\n"
    return head.encode("latin1") + body


def build_error_response():
    return (
        b"HTTP/1.1 502 Bad Gateway\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )


# --- connection handling -----------------------------------------------------

def handle(conn, addr, ctx):
    conn.settimeout(CLIENT_TIMEOUT)
    try:
        tls = ctx.wrap_socket(conn, server_side=True)
    except (ssl.SSLError, OSError) as exc:
        # A single leaf whose SAN covers both gs-loc hosts is presented, so a
        # TLS_FAIL here (with the CA trusted on the phone) means pinning, not SNI.
        log("TLS_FAIL peer=%s:%d err=%r" % (addr[0], addr[1], exc))
        _safe_close(conn)
        return

    upstream = ""
    try:
        reader = tls.makefile("rb")
        request_line, header_lines = read_headers(reader)
        req_headers = header_map(header_lines)
        body = read_body(reader, req_headers)

        parts = request_line.split(" ")
        method = parts[0] if parts else ""
        path = parts[1] if len(parts) > 1 else ""
        # Upstream host comes from the phone's HTTP/1.1 Host header (mandatory);
        # no reliance on SNI, so a single shared TLS context stays thread-safe.
        upstream = req_headers.get("host", "").split(":", 1)[0]
        if not upstream:
            raise ValueError("request has no Host header")
        if not is_allowed_host(upstream):
            raise ValueError("unexpected upstream host: %s" % upstream)

        origin = select_origin_host(upstream, method, path)
        if origin != upstream:
            log("ORIGIN_SUBSTITUTE requested=%s origin=%s path=%s"
                % (upstream, origin, operational_path(path)))
        status_line, up_header_lines, up_headers, resp_body = fetch_upstream(
            origin, request_line, header_lines, body
        )
        assist = is_assist_host(upstream)
        if assist:
            tile_state = "present" if req_headers.get("x-tilekey") else "none"
            log("ASSIST host=%s method=%s path=%s code=%s tile=%s type=%s encoding=%s req=%d resp=%d"
                % (upstream, method, operational_path(path), status_line.split(" ")[1:2],
                   tile_state,
                   up_headers.get("content-type", "-"),
                   up_headers.get("content-encoding", "identity"),
                   len(body), len(resp_body)))
        rewritten = False
        strip_content_encoding = False
        count = 0
        if (
            method == "POST"
            and path.startswith("/clls/wloc")
            and upstream in GSLOC_HOSTS
            and status_line.split(" ")[1:2] == ["200"]
        ):
            try:
                lat, lon, acc = active_target()
                new_body, count, anchor, anchor_source = gx.translate_response(
                    resp_body,
                    lat,
                    lon,
                    request_body=body,
                    accuracy=acc,
                )
                if count > 0:
                    resp_body = new_body
                    rewritten = True
                    strip_content_encoding = True
                    anchor_state = "present" if anchor is not None else "none"
                    log("TRANSLATE host=%s path=%s locations=%d anchor=%s source=%s bytes=%d"
                        % (upstream, operational_path(path), count, anchor_state,
                           anchor_source, len(new_body)))
                elif anchor_source == gx.NO_FIX_SOURCE:
                    # The decoder proved that every known Location is Apple's
                    # (-180, -180) no-fix sentinel (or the block is empty).
                    # Preserve that honest no-fix result instead of creating a
                    # batch of identical synthetic coordinates. Unknown or
                    # malformed unanchored responses never reach this branch.
                    resp_body = new_body
                    log("TRANSLATE_NOFIX_PASSTHROUGH host=%s path=%s bytes=%d"
                        % (upstream, operational_path(path), len(new_body)))
                else:
                    if FAIL_CLOSED and len(resp_body) > 10:
                        log("TRANSLATE_EMPTY_FAIL_CLOSED host=%s bytes=%d"
                            % (upstream, len(resp_body)))
                        tls.sendall(build_error_response())
                        return
                    log("TRANSLATE_EMPTY host=%s path=%s bytes=%d (unchanged)"
                        % (upstream, operational_path(path), len(resp_body)))
            except Exception as exc:
                if FAIL_CLOSED:
                    log("TRANSLATE_FAIL_CLOSED host=%s err=%r" % (upstream, exc))
                    tls.sendall(build_error_response())
                    return
                log("TRANSLATE_SKIP host=%s err=%r (returned real response)" % (upstream, exc))

        if (
            is_wifi_tile_request(upstream, method, path)
            and status_line.split(" ")[1:2] == ["200"]
        ):
            try:
                lat, lon, _acc = active_target()
                new_body, tile_count, tile_anchor = rewrite_wifi_tile_response(
                    resp_body, up_headers.get("content-encoding", ""), lat, lon
                )
                if tile_count > 0:
                    resp_body = new_body
                    rewritten = True
                    count += tile_count
                    anchor_state = "present" if tile_anchor is not None else "none"
                    log("WIFITILE_TRANSLATE host=%s devices=%d anchor=%s bytes=%d"
                        % (upstream, tile_count, anchor_state, len(new_body)))
                else:
                    log("WIFITILE_EMPTY host=%s bytes=%d (unchanged)"
                        % (upstream, len(resp_body)))
            except Exception as exc:
                if FAIL_CLOSED:
                    log("WIFITILE_FAIL_CLOSED host=%s err=%r" % (upstream, exc))
                    tls.sendall(build_error_response())
                    return
                log("WIFITILE_SKIP host=%s err=%r (returned real response)"
                    % (upstream, exc))

        tls.sendall(build_response(
            status_line, up_header_lines, up_headers, resp_body,
            strip_content_encoding=strip_content_encoding,
        ))
        if not rewritten and not assist:
            log("PASSTHRU host=%s %s %s code=%s bytes=%d"
                % (upstream, method, operational_path(path),
                   status_line.split(' ')[1:2], len(resp_body)))
    except Exception as exc:
        log("ERROR peer=%s:%d host=%s err=%r" % (addr[0], addr[1], upstream, exc))
    finally:
        _safe_close(tls)


def _safe_close(sock):
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _handle_with_slot(conn, addr, ctx):
    try:
        handle(conn, addr, ctx)
    finally:
        _worker_slots.release()


def main():
    if not (os.path.exists(LEAF_CRT) and os.path.exists(LEAF_KEY)):
        sys.exit("missing leaf material: %s / %s" % (LEAF_CRT, LEAF_KEY))
    try:
        active_target()
    except Exception as exc:
        sys.exit("invalid location target configuration: %r" % (exc,))
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
    except OSError:
        pass
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=LEAF_CRT, keyfile=LEAF_KEY)
    ctx.set_alpn_protocols(["http/1.1"])

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    log("LISTENING %s:%d leaf=%s presets=%s insecure_upstream=%s attempts=%d "
        "fail_closed=%s max_workers=%d"
        % (LISTEN_HOST, LISTEN_PORT, LEAF_CRT, PRESETS, UPSTREAM_INSECURE,
           UPSTREAM_ATTEMPTS, FAIL_CLOSED, MAX_WORKERS))
    try:
        while True:
            conn, addr = srv.accept()
            if not _worker_slots.acquire(blocking=False):
                log("BUSY peer=%s:%d workers=%d" % (addr[0], addr[1], MAX_WORKERS))
                _safe_close(conn)
                continue
            threading.Thread(
                target=_handle_with_slot, args=(conn, addr, ctx), daemon=True
            ).start()
    except KeyboardInterrupt:
        log("STOPPED")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
