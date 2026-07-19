#!/usr/bin/env python3
"""Loopback-only Telegram Bot API double used by Linux end-to-end tests."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class State:
    def __init__(self, chat_id, log_path, conflict_polls=False):
        self.chat_id = str(chat_id)
        self.log_path = Path(log_path)
        self.conflict_polls = conflict_polls
        self.lock = threading.Lock()
        self.updates = []

    def record(self, method, payload):
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"method": method, "payload": payload},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n")

    def inject(self, update):
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            raise ValueError("update must be an object with an integer update_id")
        with self.lock:
            self.updates.append(update)
            self.updates.sort(key=lambda item: item["update_id"])

    def pending(self, offset):
        with self.lock:
            return [item for item in self.updates if item["update_id"] >= offset]


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeTelegram/1"

    def log_message(self, _format, *_args):
        return

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, {"ok": True})
        else:
            self.respond(404, {"ok": False})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            self.respond(413, {"ok": False})
            return
        body = self.rfile.read(length)
        state = self.server.state
        if self.path == "/inject":
            try:
                state.inject(json.loads(body.decode("utf-8")))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                self.respond(400, {"ok": False, "description": str(exc)})
                return
            self.respond(200, {"ok": True, "result": True})
            return

        prefix = "/bot"
        if not self.path.startswith(prefix) or "/" not in self.path[len(prefix):]:
            self.respond(404, {"ok": False})
            return
        method = self.path.rsplit("/", 1)[-1]
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data;"):
            payload = {
                "content_type": content_type,
                "body_size": str(len(body)),
            }
        else:
            payload = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(
                    body.decode("utf-8"), keep_blank_values=True
                ).items()
            }
        state.record(method, payload)
        if method == "getMe":
            result = {
                "id": 123456789,
                "is_bot": True,
                "first_name": "HLE Test",
                "username": "hle_test_bot",
            }
        elif method == "getChat":
            if payload.get("chat_id") != state.chat_id:
                self.respond(400, {"ok": False, "description": "chat not found"})
                return
            result = {"id": int(state.chat_id), "type": "private"}
        elif method == "getUpdates":
            if state.conflict_polls:
                self.respond(409, {
                    "ok": False,
                    "description": "terminated by another getUpdates request",
                })
                return
            try:
                offset = int(payload.get("offset", "0"))
            except ValueError:
                offset = 0
            result = state.pending(offset)
            if not result:
                time.sleep(0.2)
        elif method == "getWebhookInfo":
            result = {"url": "", "pending_update_count": 0}
        elif method in {
            "deleteWebhook", "sendMessage", "answerCallbackQuery",
            "setMyCommands", "setChatMenuButton", "sendDocument",
        }:
            result = True
        else:
            self.respond(400, {"ok": False, "description": "unsupported method"})
            return
        self.respond(200, {"ok": True, "result": result})


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--conflict-polls", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.state = State(args.chat_id, args.log, args.conflict_polls)
    server.serve_forever()


if __name__ == "__main__":
    main()
