import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, MultiFernet


class Vault:
    def __init__(self, settings):
        keys = settings.token_encryption_key.get_secret_value().split(",")
        self.cipher = (
            MultiFernet([Fernet(k.strip().encode()) for k in keys if k.strip()]) if any(keys) else None
        )

    def encrypt(self, value):
        if not self.cipher:
            raise ValueError("Set TOKEN_ENCRYPTION_KEY before storing credentials")
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        if not self.cipher:
            raise ValueError("TOKEN_ENCRYPTION_KEY is missing")
        return json.loads(self.cipher.decrypt(value.encode()))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Public URLs must use HTTPS without credentials")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private, loopback and link-local URLs are not allowed")
    return url


def safe_url(url):
    """Strip temporary upload URL queries from audit records."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + ("?<redacted>" if parts.query else "")


def redact(value):
    if isinstance(value, dict):
        return {
            k: "<redacted>"
            if k.lower()
            in {
                "access_token",
                "refresh_token",
                "uploadtoken",
                "fbuploader_video_file_chunk",
                "client_secret",
                "system_token",
            }
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
