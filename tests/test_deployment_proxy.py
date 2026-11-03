"""Optional integration check against an official Caddy binary; never binds publicly."""

import hashlib
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("allow_admin", [False, True])
def test_proxy_keeps_media_public_and_admin_restricted(tmp_path, allow_admin):
    binary = os.environ.get("CADDY_BINARY")
    if not binary:
        pytest.skip("Set CADDY_BINARY to run the real reverse-proxy routing integration test")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert not self.headers.get("X-Authenticated-User")
            assert not self.headers.get("X-Tudelu-Proxy-Secret")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"admin-upstream")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    app_port, media_port = available_port(), available_port()
    payload = b"media-fixture"
    filename = hashlib.sha256(payload).hexdigest() + ".jpg"
    (tmp_path / filename).write_bytes(payload)
    conf = Path("deploy/Caddyfile").read_text().replace("web:8000", f"127.0.0.1:{upstream.server_port}")
    conf = conf.replace("/srv/runtime/media", tmp_path.as_posix())
    path = tmp_path / "Caddyfile"
    path.write_text(conf)
    env = os.environ | {
        "APP_BASE_URL": f"http://127.0.0.1:{app_port}",
        "MEDIA_BASE_URL": f"http://127.0.0.1:{media_port}",
        "ADMIN_ALLOWED_CIDRS": "127.0.0.1/32" if allow_admin else "203.0.113.1/32",
        "TLS_EMAIL": "admin@example.invalid",
    }
    process = subprocess.Popen(
        [binary, "run", "--config", str(path), "--adapter", "caddyfile"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with httpx.Client(timeout=2) as client:
            for _ in range(50):
                if process.poll() is not None:
                    pytest.fail("Caddy exited before readiness")
                try:
                    response = client.get(f"http://127.0.0.1:{media_port}/{filename}")
                    break
                except httpx.ConnectError:
                    time.sleep(0.1)
            else:
                pytest.fail("Caddy did not start")
            assert response.status_code == 200 and response.content == payload
            assert "immutable" in response.headers["cache-control"]
            assert client.head(f"http://127.0.0.1:{media_port}/{filename}").status_code == 200
            for suffix in ("/", "/api/session", "/.env", "/oauth/x/callback?code=fixture"):
                assert client.get(f"http://127.0.0.1:{media_port}{suffix}").status_code == 404
            assert client.post(f"http://127.0.0.1:{media_port}/{filename}").status_code == 404
            response = client.get(
                f"http://127.0.0.1:{app_port}/admin/connections",
                headers={
                    "X-Forwarded-For": "203.0.113.1",
                    "X-Authenticated-User": "spoofed",
                    "X-Tudelu-Proxy-Secret": "spoofed",
                },
            )
            assert response.status_code == (200 if allow_admin else 403)
    finally:
        process.terminate()
        process.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
