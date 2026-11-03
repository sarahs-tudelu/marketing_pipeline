"""Read Secret Manager references using Google's ADC authentication and official REST API."""

import base64
import re

PREFIX = "gsm://"
RESOURCE = re.compile(
    r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest|[A-Za-z][A-Za-z0-9_-]*)\Z"
)


class SecretLoadError(RuntimeError):
    """Safe to show in startup logs; never includes credential or provider response bodies."""


def resource_name(reference):
    name = reference.removeprefix(PREFIX)
    if not reference.startswith(PREFIX) or not RESOURCE.fullmatch(name):
        raise SecretLoadError(
            "Invalid Google Secret Manager reference; use gsm://projects/PROJECT/secrets/NAME/versions/VERSION"
        )
    if "REPLACE_" in name.upper():
        raise SecretLoadError("Replace the Google Cloud project placeholder before starting the deployment")
    return name


def google_session(credentials_file=None):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if credentials_file:
        credentials, _ = google.auth.load_credentials_from_file(credentials_file, scopes=scopes)
    else:
        credentials, _ = google.auth.default(scopes=scopes)
    return AuthorizedSession(credentials, refresh_timeout=15)


def resolve_references(values, *, backend="none", timeout=15, credentials_file=None, session_factory=None):
    """Resolve only explicit references; keep values in memory and fail closed on any error.

    One read per distinct reference per settings load. No global cache, no disk cache,
    no environment mutation and no fallback to placeholder/plaintext keys after failure.
    """
    selected = {
        key: value for key, value in values.items() if isinstance(value, str) and value.startswith(PREFIX)
    }
    if not selected:
        return dict(values)
    if backend != "google":
        raise SecretLoadError("Google Secret Manager references require SECRET_BACKEND=google")
    if not 1 <= timeout <= 60:
        raise SecretLoadError("SECRET_TIMEOUT_SECONDS must be between 1 and 60")
    names = {key: resource_name(value) for key, value in selected.items()}
    result = dict(values)
    cached = {}
    key = "authentication"
    try:
        import google_crc32c

        with session_factory() if session_factory else google_session(credentials_file) as session:
            for key, name in names.items():
                if name not in cached:
                    response = session.get(
                        "https://secretmanager.googleapis.com/v1/" + name + ":access",
                        timeout=timeout,
                        max_allowed_time=timeout * 2,
                        allow_redirects=False,
                    )
                    if response.status_code != 200:
                        raise SecretLoadError(
                            f"Google Secret Manager could not load {key.upper()} (HTTP {response.status_code})"
                        )
                    payload = response.json()["payload"]
                    data = base64.b64decode(payload["data"], validate=True)
                    if google_crc32c.value(data) != int(payload["dataCrc32c"]):
                        raise SecretLoadError(f"Google Secret Manager checksum failed for {key.upper()}")
                    value = data.decode("utf-8")
                    if not value or "\x00" in value or value.startswith(PREFIX):
                        raise SecretLoadError(
                            f"Google Secret Manager returned an invalid value for {key.upper()}"
                        )
                    cached[name] = value
                result[key] = cached[name]
    except SecretLoadError:
        raise
    except Exception:
        # Authentication/network exceptions may embed credentials or payload data.
        raise SecretLoadError(
            f"Google Secret Manager could not load {key.upper()}; check ADC, IAM and connectivity"
        ) from None
    return result
