# -*- coding: utf-8 -*-
"""Проверка MaxService: документированная отправка и ретраи только для GET.

Запуск из каталога backend:  python tests/test_max_service.py
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.max_service as max_service  # noqa: E402
from app.services.max_service import MaxService  # noqa: E402

REQUESTS = []


class _Handler(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode() or "null") if raw else None
        parsed = urlparse(self.path)
        REQUESTS.append({
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "body": body,
        })

    def do_POST(self):
        self._record()
        self._respond(200, {"ok": True})

    def do_GET(self):
        self._record()
        if self.path.startswith("/subscriptions"):
            # Первый вызов — 500 (проверяем ретрай), второй — 200.
            gets = [r for r in REQUESTS if r["path"].startswith("/subscriptions")]
            if len(gets) == 1:
                self._respond(500, {"error": "temporary"})
                return
            self._respond(200, {"subscriptions": [{"chat_id": 777, "username": "mychan", "title": "Мой канал"}]})
            return
        self._respond(200, {})

    def _respond(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_send_message_documented_shape():
    REQUESTS.clear()
    svc = MaxService(token="tok")
    ok = asyncio.run(svc.send_message("12345", "Привет", notify=False))
    assert ok is True
    assert len(REQUESTS) == 1, f"POST не должен повторяться: {len(REQUESTS)} запросов"
    req = REQUESTS[0]
    assert req["method"] == "POST"
    assert req["path"] == "/messages", req["path"]
    assert req["query"].get("chat_id") == "12345", req["query"]
    assert req["body"] == {"text": "Привет", "notify": False}, req["body"]


def test_get_is_retried_on_5xx():
    REQUESTS.clear()
    svc = MaxService(token="tok")
    cid = asyncio.run(svc.resolve_chat_id("mychan"))
    assert cid == "777", cid
    gets = [r for r in REQUESTS if r["path"].startswith("/subscriptions")]
    assert len(gets) == 2, f"GET должен ретраиться один раз: {len(gets)}"


def test_parse_chat_id_local_forms():
    svc = MaxService(token="tok")
    REQUESTS.clear()
    assert asyncio.run(svc.parse_chat_id("123")) == "123"
    assert asyncio.run(svc.parse_chat_id("https://max.ru/chat/456")) == "456"
    assert asyncio.run(svc.parse_chat_id("https://max.ru/chat/456?x=1")) == "456"
    # Negative ids (groups/channels) and web.max.ru links must work too.
    assert asyncio.run(svc.parse_chat_id("-78905088689474")) == "-78905088689474"
    assert asyncio.run(svc.parse_chat_id("https://web.max.ru/-78905088689474")) == "-78905088689474"
    assert asyncio.run(svc.parse_chat_id("https://max.ru/chat/-123")) == "-123"
    assert REQUESTS == [], "локальные формы не должны ходить в сеть"


_TESTS = [
    ("отправка: POST /messages?chat_id=..., без ретраев", test_send_message_documented_shape),
    ("GET ретраится на 5xx", test_get_is_retried_on_5xx),
    ("локальные формы chat_id без сети", test_parse_chat_id_local_forms),
]

if __name__ == "__main__":
    server, port = _start_server()
    max_service.BASE_URL = f"http://127.0.0.1:{port}"
    failures = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"OK    {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    server.shutdown()
    total = len(_TESTS)
    print(f"{total - failures}/{total} тестов пройдено")
    sys.exit(1 if failures else 0)
