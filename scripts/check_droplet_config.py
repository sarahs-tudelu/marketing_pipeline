"""Offline deployment preflight. Does not authenticate, resolve DNS, or fetch secrets."""

import argparse
import ipaddress
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from social.cloud_secrets import SecretLoadError, resource_name
from social.config import Settings


def validate(values):
    errors = []
    for key, expected in {
        "ENVIRONMENT": "production",
        "SECRET_BACKEND": "google",
        "AUTH_MODE": "basic",
    }.items():
        if values.get(key) != expected:
            errors.append(f"{key} must be {expected} for this Droplet compose file")
    backend = values.get("STORAGE_BACKEND")
    if backend not in {"local", "s3"}:
        errors.append("STORAGE_BACKEND must be local or s3")
    if backend == "s3":
        for key in ("S3_BUCKET", "S3_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not values.get(key) or "REPLACE_" in values[key].upper():
                errors.append(f"Set {key} for Spaces storage (keys must be Secret Manager references)")
        bucket = values.get("S3_BUCKET") or ""
        if any(char in bucket for char in "/:@ "):
            errors.append("S3_BUCKET must contain only the bucket name, without a URL or path")
        try:
            endpoint = urlsplit(values.get("S3_ENDPOINT_URL") or "")
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.hostname.endswith(".invalid")
                or "REPLACE_" in endpoint.netloc.upper()
                or endpoint.username
                or endpoint.password
                or endpoint.port
                or endpoint.path
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError
            if (
                endpoint.hostname.endswith(".digitaloceanspaces.com")
                and endpoint.hostname != f"{values.get('S3_REGION')}.digitaloceanspaces.com"
            ):
                raise ValueError
        except ValueError:
            errors.append(
                "Set S3_ENDPOINT_URL to the HTTPS regional API origin, e.g. https://nyc3.digitaloceanspaces.com; omit bucket/CDN/path"
            )
    for key in ("APP_BASE_URL", "MEDIA_BASE_URL"):
        url = urlsplit(values.get(key) or "")
        if (
            url.scheme != "https"
            or not url.hostname
            or url.hostname.endswith(".invalid")
            or url.username
            or url.password
            or url.port
            or url.path
            or url.query
            or url.fragment
        ):
            errors.append(f"Replace {key} with a real HTTPS origin, without a path or trailing slash")
    if values.get("APP_BASE_URL") == values.get("MEDIA_BASE_URL"):
        errors.append("Use different app and media origins")
    if not values.get("TLS_EMAIL") or values["TLS_EMAIL"].endswith(".invalid"):
        errors.append("Replace TLS_EMAIL with the certificate contact address")
    try:
        cidrs = [ipaddress.ip_network(part) for part in (values.get("ADMIN_ALLOWED_CIDRS") or "").split()]
        if not cidrs or all(c.is_loopback for c in cidrs) or any(c.prefixlen == 0 for c in cidrs):
            raise ValueError
    except ValueError:
        errors.append(
            "Set ADMIN_ALLOWED_CIDRS to the company VPN/office egress networks; do not allow the entire internet"
        )
    try:
        db = make_url(values.get("DATABASE_URL") or "")
        if (
            db.drivername != "postgresql+psycopg"
            or not all((db.username, db.host, db.port, db.database))
            or db.host in {"db", "localhost", "127.0.0.1", "::1"}
            or db.host.endswith(".invalid")
            or db.password
            or "REPLACE_" in str(db).upper()
            or not 1 <= db.port <= 65535
        ):
            raise ValueError
        if set(db.query) - {
            "sslmode",
            "sslrootcert",
            "connect_timeout",
            "application_name",
            "target_session_attrs",
        }:
            errors.append(
                "DATABASE_URL contains unsupported query settings; keep credentials in DATABASE_PASSWORD"
            )
        if db.query.get("sslmode") != "verify-full":
            errors.append("Managed PostgreSQL requires sslmode=verify-full in this deployment")
        cert = db.query.get("sslrootcert", "")
        if cert != "system" and (
            not isinstance(cert, str)
            or PurePosixPath(cert).parent != PurePosixPath("/run/database-certs")
            or not PurePosixPath(cert).name
        ):
            errors.append(
                "Set sslrootcert to /run/database-certs/YOUR_CA_FILE or system for Advanced Edition"
            )
        try:
            if not 1 <= int(db.query.get("connect_timeout", "0")) <= 60:
                raise ValueError
        except (ValueError, TypeError):
            errors.append("Set DATABASE_URL connect_timeout between 1 and 60 seconds")
    except Exception:
        errors.append(
            "Replace DATABASE_URL with the managed cluster's host, numeric port, username and database; omit the password"
        )
    if not (values.get("DATABASE_CA_HOST_DIR") or "").startswith("/"):
        errors.append(
            "DATABASE_CA_HOST_DIR must be an absolute Linux directory, mounted at /run/database-certs"
        )
    for key in ("DATABASE_PASSWORD", "TOKEN_ENCRYPTION_KEY", "SESSION_SECRET", "ADMIN_PASSWORD"):
        if not values.get(key):
            errors.append(f"{key} must reference an existing Secret Manager version")
    if values.get("AI_MODE") == "live":
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            if not values.get(key):
                errors.append(f"Live AI requires {key}")
    for name, field in Settings.model_fields.items():
        key = name.upper()
        if field.annotation is SecretStr and values.get(key):
            try:
                resource_name(values[key])
                if (
                    key in {"DATABASE_PASSWORD", "TOKEN_ENCRYPTION_KEY"}
                    and not values[key].rsplit("/", 1)[-1].isdigit()
                ):
                    errors.append(f"Pin {key} to a numeric version for controlled rotation")
            except SecretLoadError:
                errors.append(
                    f"Replace {key} with a valid gsm://projects/PROJECT/secrets/NAME/versions/VERSION reference"
                )
    if not (values.get("GCP_ADC_HOST_PATH") or "").startswith("/"):
        errors.append("GCP_ADC_HOST_PATH must be an absolute Linux path outside the repository")
    if values.get("GOOGLE_APPLICATION_CREDENTIALS") != "/run/secrets/google_adc":
        errors.append("GOOGLE_APPLICATION_CREDENTIALS must match /run/secrets/google_adc")
    for key, expected in {"MEDIA_DIR": "/app/data/media", "WATCH_DIR": "/app/data/inbox"}.items():
        if values.get(key) != expected:
            errors.append(f"{key} must be {expected} to match the shared runtime volume")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--environment", action="store_true", help="Check already-exported Compose environment variables"
    )
    parser.add_argument(
        "--check-adc-file", action="store_true", help="Also check the bootstrap file on the actual Droplet"
    )
    parser.add_argument(
        "--check-ca-files",
        action="store_true",
        help="Check managed PostgreSQL trust configuration on this host",
    )
    args = parser.parse_args()
    values = dict(os.environ) if args.environment else dotenv_values(args.env_file)
    errors = validate(values)
    if args.check_adc_file:
        path = Path(
            values.get("GOOGLE_APPLICATION_CREDENTIALS" if args.environment else "GCP_ADC_HOST_PATH") or ""
        )
        if not path.is_file():
            errors.append("ADC bootstrap file is missing on this host")
        elif path.stat().st_mode & 0o007:
            errors.append(
                "ADC bootstrap file must not be accessible to other users (use root:10001 mode 0440)"
            )
    if args.check_ca_files:
        errors.extend(validate_ca_files(values, in_container=args.environment))
    if errors:
        print("Deployment placeholders/settings still need attention:")
        for error in errors:
            print("- " + error)
        return 1
    print("Offline deployment configuration passed. No secrets or external services were accessed.")
    return 0


def validate_ca_files(values, in_container=False):
    import ssl

    try:
        cert = make_url(values.get("DATABASE_URL") or "").query.get("sslrootcert", "")
    except Exception:
        return [
            "CA check could not start: correct the DATABASE_URL format first; certificate has not been read"
        ]
    try:
        directory = Path("/run/database-certs" if in_container else values.get("DATABASE_CA_HOST_DIR", ""))
        if not directory.is_dir():
            return ["Create the managed database certificate directory before starting Compose"]
        if cert == "system":
            from psycopg import pq

            return [] if pq.version() >= 160000 else ["sslrootcert=system requires libpq 16 or newer"]
        path = Path(cert) if in_container else directory / PurePosixPath(cert).name
        if not path.is_file():
            return ["Download the cluster CA certificate and place it in DATABASE_CA_HOST_DIR"]
        ssl.create_default_context(cafile=str(path))
        return []
    except Exception:
        return ["Could not validate managed PostgreSQL CA configuration; check the URL and certificate file"]


if __name__ == "__main__":
    raise SystemExit(main())
