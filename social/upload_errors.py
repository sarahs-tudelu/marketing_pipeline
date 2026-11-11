"""Safe admin-facing errors: never expose provider bodies, signed URLs or credentials."""

import errno

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError
from sqlalchemy.exc import SQLAlchemyError


def upload_error_details(exc):
    details = {"error_type": type(exc).__name__}
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        # Keep useful diagnostics without logging arbitrary provider bodies or credentials.
        known_codes = {
            "NoSuchBucket",
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
            "NoSuchKey",
            "NotFound",
            "404",
            "MissingContentLength",
            "NotImplemented",
            "InvalidRequest",
            "XAmzContentSHA256Mismatch",
            "BadDigest",
            "InvalidDigest",
            "SlowDown",
            "InternalError",
            "ServiceUnavailable",
            "RequestTimeout",
        }
        details["storage_error_code"] = code if code in known_codes else "Unknown"
    if isinstance(exc, OSError):
        details["errno"] = exc.errno
    return details


def describe_upload_error(exc):
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            return 503, "The media bucket was not found. Check S3_BUCKET and the region in S3_ENDPOINT_URL."
        if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return (
                503,
                "Storage denied the upload. Check the Spaces credentials and bucket write permissions.",
            )
        if code in {"NoSuchKey", "NotFound", "404"}:
            return 400, "The bucket object was not found. Check its exact object key."
        if code in {"MissingContentLength", "NotImplemented", "InvalidRequest", "XAmzContentSHA256Mismatch"}:
            return (
                503,
                "Storage rejected the upload format. Deploy the current fixed-length Spaces upload code and check the regional endpoint.",
            )
        if code in {"BadDigest", "InvalidDigest"}:
            return (
                503,
                "Storage rejected the file checksum. Retry the original file after checking the storage connection.",
            )
        return (
            503,
            "Media storage could not complete the upload. Check the bucket, endpoint and storage service status.",
        )
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return 503, "Storage credentials are missing. Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
    if isinstance(exc, BotoCoreError):
        return (
            503,
            "Could not reach media storage. Check its credentials, regional endpoint and server connectivity.",
        )
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return (
                503,
                "The server ran out of temporary or media disk space. Free space before uploading again.",
            )
        if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            return (
                503,
                "The server cannot write the upload. Check permissions on its temporary directory and MEDIA_DIR.",
            )
        return 503, "The server could not read or save the file. Check temporary/media storage and retry."
    if isinstance(exc, SQLAlchemyError):
        return (
            503,
            "The media record could not be saved. Check the database connection and apply pending migrations.",
        )
    if isinstance(exc, ValueError):
        return 400, str(exc)
    return (
        500,
        "The upload could not finish. Check the asset_upload_failed event in the server logs and retry.",
    )
