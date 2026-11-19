"""S3/R2 configuration and access adapted from bureau14/qdb-artifacts-buildkite-plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore import exceptions
from botocore.exceptions import ClientError
from files import hash_stream, open_regular
from plugin_config import CacheConfig

BACKEND_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/backend"
DESTINATION_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/destination"
ENDPOINT_URL_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/endpoint-url"
R2_ACCOUNT_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/account-id"
R2_ACCESS_KEY_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/access-key-id"
R2_SECRET_ACCESS_KEY_SSM_PARAM = "/services/buildkite/credentials/artifacts/r2/secret-access-key"

# File workers share one client. Parts run serially inside a worker, so streaming
# bodies and multipart requests use the same four request slots as small files.
MAX_WORKERS = 4
_TRANSFER = TransferConfig(use_threads=False, preferred_transfer_client="classic")
_BODY_ATTEMPTS = 2
_STREAM_ERRORS = (exceptions.HTTPClientError, exceptions.IncompleteReadError)


@dataclass(frozen=True)
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None


def _ssm_get_optional(ssm, name, with_decryption=True):
    """Return SSM parameter value or None if absent. Re-raises non-NotFound errors
    so IAM misconfigurations surface explicitly."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=with_decryption)["Parameter"]["Value"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ParameterNotFound":
            return None
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        raise RuntimeError(f"failed to read SSM parameter {name}: {code} — {msg}") from exc


# Prefixes that indicate a placeholder / unconfigured value rather than a real one.
_PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")


def _check_placeholder(value, label):
    """Reject credentials that still contain an unconfigured placeholder."""
    if value and any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        raise ValueError(
            f"{label} contains an unconfigured placeholder — "
            f"replace it with the real credential in Terraform tfvars or SSM"
        )


def _env_or_ssm(ssm, env_name, ssm_name, with_decryption=True):
    """Env var with SSM fallback. Env takes precedence for per-job overrides."""
    return os.environ.get(env_name) or _ssm_get_optional(
        ssm, ssm_name, with_decryption=with_decryption
    )


def load_store_config(ssm):
    """Resolve backend config: env vars → SSM → defaults.
    For R2, endpoint URL defaults to https://{account_id}.r2.cloudflarestorage.com."""
    backend = (
        (os.environ.get("ARTIFACTS_BACKEND") or _ssm_get_optional(ssm, BACKEND_SSM_PARAM) or "s3")
        .strip()
        .lower()
    )

    if backend not in {"s3", "r2"}:
        raise ValueError(
            f"unsupported artifact backend: {backend!r} (expected 's3' or 'r2'). "
            f"Check env var ARTIFACTS_BACKEND or SSM parameter {BACKEND_SSM_PARAM}"
        )

    destination = os.environ.get("ARTIFACTS_DESTINATION") or _ssm_get_optional(
        ssm, DESTINATION_SSM_PARAM
    )
    if not destination:
        raise ValueError(
            f"artifact destination not configured. "
            f"Set env var ARTIFACTS_DESTINATION or SSM parameter {DESTINATION_SSM_PARAM}"
        )

    # Applies to both backends; mainly for local MinIO / dev setups.
    endpoint_url = os.environ.get("ARTIFACTS_ENDPOINT_URL")

    if backend == "r2":
        endpoint_url = endpoint_url or _ssm_get_optional(ssm, ENDPOINT_URL_SSM_PARAM)

        r2_account_id = _env_or_ssm(ssm, "ARTIFACTS_R2_ACCOUNT_ID", R2_ACCOUNT_ID_SSM_PARAM)
        if not r2_account_id:
            raise ValueError(
                f"R2 backend requires Cloudflare account ID. "
                f"Set env var ARTIFACTS_R2_ACCOUNT_ID or SSM parameter {R2_ACCOUNT_ID_SSM_PARAM}"
            )
        _check_placeholder(r2_account_id, f"R2 account ID (SSM {R2_ACCOUNT_ID_SSM_PARAM})")

        r2_access_key_id = _env_or_ssm(
            ssm,
            "ARTIFACTS_R2_ACCESS_KEY_ID",
            R2_ACCESS_KEY_ID_SSM_PARAM,
        )
        if not r2_access_key_id:
            raise ValueError(
                f"R2 backend requires access key ID. "
                f"Set env var ARTIFACTS_R2_ACCESS_KEY_ID or SSM parameter {R2_ACCESS_KEY_ID_SSM_PARAM}"
            )
        _check_placeholder(r2_access_key_id, f"R2 access key ID (SSM {R2_ACCESS_KEY_ID_SSM_PARAM})")

        r2_secret_access_key = _env_or_ssm(
            ssm,
            "ARTIFACTS_R2_SECRET_ACCESS_KEY",
            R2_SECRET_ACCESS_KEY_SSM_PARAM,
            with_decryption=True,
        )
        if not r2_secret_access_key:
            raise ValueError(
                f"R2 backend requires secret access key. "
                f"Set env var ARTIFACTS_R2_SECRET_ACCESS_KEY or SSM SecureString {R2_SECRET_ACCESS_KEY_SSM_PARAM}"
            )
        _check_placeholder(
            r2_secret_access_key,
            f"R2 secret access key (SSM {R2_SECRET_ACCESS_KEY_SSM_PARAM})",
        )

        if not endpoint_url:
            endpoint_url = f"https://{r2_account_id}.r2.cloudflarestorage.com"

        return StoreConfig(
            backend=backend,
            destination=destination,
            endpoint_url=endpoint_url,
            r2_access_key_id=r2_access_key_id,
            r2_secret_access_key=r2_secret_access_key,
        )

    return StoreConfig(
        backend=backend,
        destination=destination,
        endpoint_url=endpoint_url,
    )


def parse_s3(uri):
    """Resolve a destination URI or the legacy plain bucket value before opening S3."""
    bucket, prefix = uri, ""
    if uri.startswith("s3://"):
        bucket, _, prefix = uri[5:].partition("/")
    # Preserve legacy bucket names accepted by the SDK, including uppercase
    # letters and underscores. Credentials, ports, and URI query fields have
    # no meaning in a cache destination.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", bucket) or "?" in uri or "#" in uri:
        raise ValueError("artifact destination must be a plain bucket name or s3://bucket/prefix")
    return bucket, prefix.strip("/")


def _s3_client(cfg, operation: CacheConfig):
    """Create an S3-compatible client using the artifacts plugin's backend settings.

    R2 needs signature_version=s3v4 and addressing_style=path (R2 endpoints are
    account-level, not bucket-level — virtual-host style would produce invalid hostnames).
    """
    # Both backends use the manifest SHA-256 for end-to-end file verification.
    client_options = {"request_checksum_calculation": "when_required"}
    if cfg.backend == "r2":
        client_options.update(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )
    kwargs = {"config": sdk_config(operation, **client_options)}
    if cfg.backend == "r2":
        kwargs["region_name"] = "auto"
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    if cfg.backend == "r2":
        kwargs["aws_access_key_id"] = cfg.r2_access_key_id
        kwargs["aws_secret_access_key"] = cfg.r2_secret_access_key

    return boto3.client("s3", **kwargs)


def sdk_config(operation: CacheConfig, **kwargs):
    return Config(
        connect_timeout=operation.connect_timeout,
        read_timeout=operation.read_timeout,
        retries={"mode": "standard", "total_max_attempts": operation.retries},
        **kwargs,
    )


def aws_clients(operation: CacheConfig):
    """Return the SSM client for config resolution; transfers own a separate S3 client."""
    session = boto3.session.Session()
    region_name = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    return session.client("ssm", config=sdk_config(operation, region_name=region_name))


class LazySsmClient:
    """Create SSM client only if config resolution actually needs SSM."""

    def __init__(self, operation: CacheConfig):
        self._operation = operation
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            self._client = aws_clients(self._operation)
        return self._client

    def get_parameter(self, *args, **kwargs):
        return self._ensure_client().get_parameter(*args, **kwargs)

    def close(self):
        if self._client is not None:
            self._client.close()


def open_store(operation: CacheConfig):
    ssm = LazySsmClient(operation)
    try:
        cfg = load_store_config(ssm)
        bucket, prefix = parse_s3(cfg.destination)
        client = _s3_client(cfg, operation)
        return client, bucket, prefix
    finally:
        ssm.close()


def _manifest_fields(pairs):
    # A repeated field can assign different layouts or destinations to the same
    # document depending on the JSON decoder. Such a candidate is an error.
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate JSON member in cache manifest")
    return fields


def _invalid_constant(token):
    raise ValueError(f"invalid JSON number in cache manifest: {token}")


def get_manifest(client, bucket: str, key: str, *, tolerate_invalid=False):
    """Read one snapshot, allowing a forced save to rebuild an undecodable document."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # An untyped HTTP 404 can indicate an incorrect endpoint.
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise
    with closing(response["Body"]) as body:
        raw = body.read()
    try:
        document = json.loads(
            raw, object_pairs_hook=_manifest_fields, parse_constant=_invalid_constant
        )
        if not isinstance(document, dict):
            raise ValueError("cache manifest must be a JSON object")
    except (ValueError, RecursionError):
        if not tolerate_invalid:
            raise
        return None
    return document


def publish_manifest(client, bucket: str, key: str, manifest: dict):
    """Publish the complete snapshot. The last successful write selects the visible manifest."""
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def upload_payload(
    client, bucket: str, key: str, path: Path, size: int, digest: str, tmp_dir: Path
):
    """Capture and verify private bytes before the SDK opens the file for upload or retry."""
    with tempfile.TemporaryDirectory(dir=tmp_dir) as staging:
        captured = Path(staging) / "payload"
        with open_regular(path) as source, captured.open("wb") as output:
            if hash_stream(source, output) != (digest, size):
                raise ValueError(f"source changed during payload upload: {path}")
        client.upload_file(str(captured), bucket, key, Config=_TRANSFER)
    return size


def download_payload(
    client, bucket: str, key: str, path: Path, size: int, digest: str, *, on_bytes=None
):
    """Fetch the immutable key from the manifest, retry interrupted bodies, and verify the bytes."""
    transferred = 0
    # Each GET has its own configured SDK request attempts. Once the SDK hands
    # us a response, an interrupted body needs a fresh GET for the same key.
    for attempt in range(1, _BODY_ATTEMPTS + 1):
        response = client.get_object(Bucket=bucket, Key=key)
        try:
            with closing(response["Body"]) as body:
                if response["ContentLength"] != size:
                    raise ValueError(f"payload length differs from manifest: {key}")
                checksum = hashlib.sha256()
                with path.open("wb") as output:
                    for chunk in iter(lambda: body.read(_TRANSFER.io_chunksize), b""):
                        transferred += len(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
                        output.write(chunk)
                        checksum.update(chunk)
                    if output.tell() != size or checksum.hexdigest() != digest:
                        raise ValueError(f"payload length or SHA-256 differs from manifest: {key}")
            return transferred
        except _STREAM_ERRORS:
            if attempt == _BODY_ATTEMPTS:
                raise
