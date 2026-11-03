import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import google_crc32c
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from pydantic import ValidationError

from social.cloud_secrets import SecretLoadError, resolve_references
from social.config import Settings

REF = "gsm://projects/test-project/secrets/test-key/versions/1"


class Session:
    def __init__(self, value="mock-value", status=200, crc=None):
        self.value, self.status, self.crc, self.calls = value, status, crc, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        data = self.value.encode()
        return SimpleNamespace(
            status_code=self.status,
            json=lambda: {
                "payload": {
                    "data": base64.b64encode(data).decode(),
                    "dataCrc32c": str(google_crc32c.value(data) if self.crc is None else self.crc),
                }
            },
        )


def test_explicit_secret_references_deduplicated_in_memory():
    client = Session()
    values = {"openai_api_key": REF, "anthropic_api_key": REF, "blank": ""}
    result = resolve_references(values, backend="google", session_factory=lambda: client)
    assert result == {"openai_api_key": "mock-value", "anthropic_api_key": "mock-value", "blank": ""}
    assert len(client.calls) == 1
    assert (
        client.calls[0][0]
        == "https://secretmanager.googleapis.com/v1/projects/test-project/secrets/test-key/versions/1:access"
    )
    assert client.calls[0][1]["allow_redirects"] is False
    assert values["openai_api_key"] == REF


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_secret_manager_failure_is_closed_and_redacted(status):
    with pytest.raises(SecretLoadError) as exc:
        resolve_references(
            {"openai_api_key": REF}, backend="google", session_factory=lambda: Session("DO-NOT-LOG", status)
        )
    assert "OPENAI_API_KEY" in str(exc.value)
    assert "DO-NOT-LOG" not in str(exc.value)


def test_bad_checksum_and_invalid_payload_rejected():
    for client in [Session("never-print", crc=0), Session("")]:
        with pytest.raises(SecretLoadError):
            resolve_references({"key": REF}, backend="google", session_factory=lambda: client)


@pytest.mark.parametrize(
    "ref",
    [
        "gsm://projects/REPLACE_GCP_PROJECT/secrets/key/versions/1",
        "gsm://bad",
        "gsm://projects/p/secrets/../versions/1",
    ],
)
def test_placeholders_and_invalid_references_fail_before_authentication(ref):
    def forbidden():
        pytest.fail("Must not try real credentials for placeholder configuration")

    with pytest.raises(SecretLoadError):
        resolve_references({"key": ref}, backend="google", session_factory=forbidden)


def test_backend_opt_in_and_credential_errors_sanitized():
    with pytest.raises(SecretLoadError, match="SECRET_BACKEND"):
        resolve_references({"key": REF})

    def broken():
        raise RuntimeError("private-key-material")

    with pytest.raises(SecretLoadError) as exc:
        resolve_references({"key": REF}, backend="google", session_factory=broken)
    assert "private-key-material" not in str(exc.value)
    assert resolve_references({"blank": ""}, session_factory=broken) == {"blank": ""}


@pytest.mark.parametrize("timeout", [-1, 100000, float("nan")])
def test_invalid_timeout_rejected_before_any_cloud_call(timeout):
    with pytest.raises(SecretLoadError, match="SECRET_TIMEOUT_SECONDS"):
        resolve_references(
            {"key": REF},
            backend="google",
            timeout=timeout,
            session_factory=lambda: pytest.fail("Invalid timeout reached authentication"),
        )


def test_settings_load_secrets_before_production_validation_and_hide_database_password(monkeypatch):
    payloads = {
        "db": "p@ss:/?#word",
        "session": "session-fixture",
        "admin": "admin-fixture",
        "key": Fernet.generate_key().decode(),
    }
    client = Session()

    def get(url, **kwargs):
        client.value = payloads[url.split("/secrets/")[1].split("/")[0]]
        return Session.get(client, url, **kwargs)

    monkeypatch.setattr(client, "get", get)
    monkeypatch.setattr("social.cloud_secrets.google_session", lambda *args: client)
    refs = {
        name: REF.replace("test-key", short)
        for name, short in {
            "database_password": "db",
            "session_secret": "session",
            "admin_password": "admin",
            "token_encryption_key": "key",
        }.items()
    }
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_backend="google",
        auth_mode="basic",
        admin_username="admin",
        database_url="postgresql+psycopg://social@db:5432/social",
        **refs,
    )
    assert settings.database_dsn.password == payloads["db"]
    assert payloads["db"] not in str(settings.database_dsn)
    assert payloads["db"] not in repr(settings)
    assert settings.database_url == "postgresql+psycopg://social@db:5432/social"


def test_dotenv_precedence_and_local_override(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    path.write_text("SECRET_BACKEND=google\nOPENAI_API_KEY=" + REF + "\n")
    monkeypatch.setenv("TUDELU_ENV_FILE", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "explicit-environment-value")
    monkeypatch.setattr(
        "social.cloud_secrets.google_session", lambda *args: pytest.fail("No remote reference selected")
    )
    assert Settings().openai_api_key.get_secret_value() == "explicit-environment-value"
    assert Settings(_env_file=None).secret_backend == "none"


def test_validation_errors_do_not_echo_plaintext_secrets():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, environment="production", openai_api_key="never-print")
    assert "never-print" not in str(exc.value)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_template_fails_preflight_until_placeholders_are_replaced():
    module = load_module("preflight", "scripts/check_droplet_config.py")
    values = dict(dotenv_values("deploy/droplet.env.example"))
    assert "REPLACE_WITH_GCP_PROJECT_ID" in values["DATABASE_PASSWORD"]
    assert module.validate(values)
    values.update(
        APP_BASE_URL="https://social.example.org",
        MEDIA_BASE_URL="https://media.example.org",
        TLS_EMAIL="admin@example.org",
        ADMIN_ALLOWED_CIDRS="203.0.113.1/32",
        DATABASE_URL="postgresql+psycopg://app_user@cluster.db.ondigitalocean.com:25060/app_db?sslmode=verify-full&sslrootcert=/run/database-certs/ca.pem&connect_timeout=10",
    )
    values = {
        k: v.replace("REPLACE_WITH_GCP_PROJECT_ID", "test-project") if v else v for k, v in values.items()
    }
    assert module.validate(values) == []
    values.update(
        STORAGE_BACKEND="s3",
        S3_BUCKET="tudelu-test-media",
        S3_ENDPOINT_URL="https://nyc3.digitaloceanspaces.com",
        S3_REGION="nyc3",
        AWS_ACCESS_KEY_ID=REF,
        AWS_SECRET_ACCESS_KEY=REF.replace("test-key", "spaces-secret"),
    )
    assert module.validate(values) == []
    values["S3_ENDPOINT_URL"] = "https://tudelu-test-media.nyc3.digitaloceanspaces.com"
    assert any("S3_ENDPOINT_URL" in error for error in module.validate(values))
    values["S3_ENDPOINT_URL"] = "https://nyc3.digitaloceanspaces.com"
    values["AWS_SECRET_ACCESS_KEY"] = ""
    assert any("AWS_SECRET_ACCESS_KEY" in error for error in module.validate(values))
    values["AWS_SECRET_ACCESS_KEY"] = REF.replace("test-key", "spaces-secret")
    values["OPENAI_API_KEY"] = "plaintext-not-allowed"
    assert any("OPENAI_API_KEY" in error for error in module.validate(values))


def test_droplet_compose_only_exposes_https_proxy():
    import yaml

    config = yaml.safe_load(Path("compose.droplet.yaml").read_text())
    assert {name for name, service in config["services"].items() if service.get("ports")} == {"proxy"}
    assert config["services"]["proxy"]["ports"] == ["80:80", "443:443"]
    assert "db" not in config["services"] and "postgres" not in config["volumes"]
    assert "depends_on" not in config["services"]["migrate"]
    for name in ("web", "worker", "migrate"):
        mount = next(m for m in config["services"][name]["volumes"] if isinstance(m, dict))
        assert mount["target"] == "/run/database-certs" and mount["read_only"]
        assert mount["bind"]["create_host_path"] is False
    assert config["services"]["web"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize(
    "suffix",
    [
        "sslmode=require&sslrootcert=/run/database-certs/ca.pem&connect_timeout=10",
        "sslmode=verify-full&connect_timeout=10",
        "sslmode=verify-full&sslrootcert=/wrong/path/ca.pem&connect_timeout=10",
        "sslmode=verify-full&sslrootcert=system&connect_timeout=0",
        "sslmode=verify-full&sslrootcert=system&connect_timeout=10&password=wrong-place",
        "sslmode=verify-full&sslrootcert=system&connect_timeout=10&sslmode=require",
    ],
)
def test_managed_database_preflight_rejects_insecure_or_ambiguous_settings(suffix):
    module = load_module("preflight", "scripts/check_droplet_config.py")
    values = dict(dotenv_values("deploy/droplet.env.example"))
    values["DATABASE_URL"] = (
        "postgresql+psycopg://app_user@cluster.db.ondigitalocean.com:25060/app_db?" + suffix
    )
    assert any("ssl" in error.lower() or "DATABASE_URL" in error for error in module.validate(values))


def test_managed_database_tls_parameters_and_secret_password_reach_psycopg_without_network():
    from social.db import make_engine

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://app_user@cluster.db.ondigitalocean.com:25060/app_db?sslmode=verify-full&sslrootcert=/run/database-certs/ca.pem&connect_timeout=10",
        database_password="test-password-only",
    )
    engine = make_engine(settings.database_dsn)
    try:
        _, arguments = engine.dialect.create_connect_args(engine.url)
        assert arguments["sslmode"] == "verify-full"
        assert arguments["sslrootcert"] == "/run/database-certs/ca.pem"
        assert arguments["connect_timeout"] == "10"
        assert arguments["password"] == "test-password-only"
        assert "test-password-only" not in str(engine.url)
    finally:
        engine.dispose()


def test_managed_database_ca_preflight_checks_file_and_system_support(tmp_path, monkeypatch):
    module = load_module("preflight", "scripts/check_droplet_config.py")
    values = {
        "DATABASE_CA_HOST_DIR": str(tmp_path),
        "DATABASE_URL": "postgresql+psycopg://user@db.example.org:25060/name?sslrootcert=/run/database-certs/missing.pem",
    }
    assert module.validate_ca_files(values)
    values["DATABASE_URL"] = "postgresql+psycopg://user@db.example.org:25060/name?sslrootcert=system"
    monkeypatch.setattr("psycopg.pq.version", lambda: 180000)
    assert module.validate_ca_files(values) == []
    monkeypatch.setattr("psycopg.pq.version", lambda: 150000)
    assert module.validate_ca_files(values)
    values["DATABASE_URL"] = "host-without-a-database-url"
    errors = module.validate_ca_files(values)
    assert "certificate has not been read" in errors[0]
