"""S3/R2 configuration and access adapted from bureau14/qdb-artifacts-buildkite-plugin."""

from __future__ import annotations

import json
import os
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from files import hash_stream
from plugin_config import CacheConfig

BACKEND_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/backend"
DESTINATION_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/destination"
ENDPOINT_URL_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/endpoint-url"
R2_ACCOUNT_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/account-id"
R2_ACCESS_KEY_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/access-key-id"
R2_SECRET_ACCESS_KEY_SSM_PARAM = "/services/buildkite/credentials/artifacts/r2/secret-access-key"

# File workers share one client. Serial multipart transfers keep one request per worker.
_TRANSFER = TransferConfig(use_threads=False, preferred_transfer_client="classic")


@dataclass(frozen=True)
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None


def _ssm_get_optional(ssm, name):
    """Return SSM parameter value or None if absent. Re-raises non-NotFound errors
    so IAM misconfigurations surface explicitly."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ParameterNotFound":
            return None
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        raise RuntimeError(f"failed to read SSM parameter {name}: {code} — {msg}") from exc


# Prefixes that indicate a placeholder / unconfigured value rather than a real one.
_PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")


def _required_r2_setting(get_parameter, env_name, ssm_name, label):
    value = os.environ.get(env_name) or get_parameter(ssm_name)
    if not value:
        raise ValueError(
            f"R2 backend requires {label}. Set env var {env_name} or SSM parameter {ssm_name}"
        )
    if value.startswith(_PLACEHOLDER_PREFIXES):
        raise ValueError(
            f"R2 {label} (SSM {ssm_name}) contains an unconfigured placeholder — "
            f"replace it with the real credential in Terraform tfvars or SSM"
        )
    return value


def load_store_config(get_parameter):
    """Resolve backend config: env vars → SSM → defaults.
    For R2, endpoint URL defaults to https://{account_id}.r2.cloudflarestorage.com."""
    backend = (
        (os.environ.get("ARTIFACTS_BACKEND") or get_parameter(BACKEND_SSM_PARAM) or "s3")
        .strip()
        .lower()
    )

    if backend not in {"s3", "r2"}:
        raise ValueError(
            f"unsupported artifact backend: {backend!r} (expected 's3' or 'r2'). "
            f"Check env var ARTIFACTS_BACKEND or SSM parameter {BACKEND_SSM_PARAM}"
        )

    destination = os.environ.get("ARTIFACTS_DESTINATION") or get_parameter(DESTINATION_SSM_PARAM)
    if not destination:
        raise ValueError(
            f"artifact destination not configured. "
            f"Set env var ARTIFACTS_DESTINATION or SSM parameter {DESTINATION_SSM_PARAM}"
        )

    # Applies to both backends; mainly for local MinIO / dev setups.
    endpoint_url = os.environ.get("ARTIFACTS_ENDPOINT_URL")

    if backend == "r2":
        endpoint_url = endpoint_url or get_parameter(ENDPOINT_URL_SSM_PARAM)

        r2_account_id = _required_r2_setting(
            get_parameter, "ARTIFACTS_R2_ACCOUNT_ID", R2_ACCOUNT_ID_SSM_PARAM, "account ID"
        )
        r2_access_key_id = _required_r2_setting(
            get_parameter, "ARTIFACTS_R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID_SSM_PARAM, "access key ID"
        )
        r2_secret_access_key = _required_r2_setting(
            get_parameter,
            "ARTIFACTS_R2_SECRET_ACCESS_KEY",
            R2_SECRET_ACCESS_KEY_SSM_PARAM,
            "secret access key",
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
    client_options = {
        "request_checksum_calculation": "when_required",
        "max_pool_connections": operation.parallelism,
    }
    kwargs = {}
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    if cfg.backend == "r2":
        client_options.update(signature_version="s3v4", s3={"addressing_style": "path"})
        kwargs.update(
            region_name="auto",
            aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
        )
    return boto3.client("s3", config=sdk_config(operation, **client_options), **kwargs)


def sdk_config(operation: CacheConfig, **kwargs):
    return Config(
        connect_timeout=operation.connect_timeout,
        read_timeout=operation.read_timeout,
        retries={"mode": "standard", "total_max_attempts": operation.retries},
        **kwargs,
    )


def open_store(operation: CacheConfig):
    """Resolve settings with SSM on demand, then open the configured object store."""
    ssm = None

    def get_parameter(name):
        nonlocal ssm
        if ssm is None:
            region = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
            ssm = boto3.session.Session().client(
                "ssm", config=sdk_config(operation, region_name=region)
            )
        return _ssm_get_optional(ssm, name)

    try:
        cfg = load_store_config(get_parameter)
    finally:
        if ssm is not None:
            ssm.close()
    bucket, prefix = parse_s3(cfg.destination)
    return _s3_client(cfg, operation), bucket, prefix


def get_manifest(client, bucket: str, key: str) -> tuple[dict | None, str | None]:
    """Read the snapshot and its ETag together so a writer can condition its publication."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # An untyped HTTP 404 can indicate an incorrect endpoint.
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None, None
        raise
    with closing(response["Body"]) as body:
        document = json.load(body)
    if not isinstance(document, dict):
        raise ValueError("cache manifest must be a JSON object")
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag or etag == "*":
        raise ValueError("object store returned no usable ETag for cache manifest")
    return document, etag


def publish_manifest(client, bucket: str, key: str, manifest: dict, etag: str | None) -> bool:
    """Commit against the version read at save time; a concurrent change skips publication."""
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    condition = {"IfNoneMatch": "*"} if etag is None else {"IfMatch": etag}
    try:
        client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json", **condition
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"PreconditionFailed", "412", "ConditionalRequestConflict"} or (
            etag is not None and code == "NoSuchKey"
        ):
            # Re-reading and retrying would replace the winner with this stale snapshot.
            return False
        raise
    return True


def upload_payload(client, bucket: str, key: str, path: Path):
    # Save inputs stay stable from hashing through the SDK's last read.
    client.upload_file(str(path), bucket, key, Config=_TRANSFER)


def download_payload(client, bucket: str, key: str, path: Path, size: int, digest: str) -> int:
    """Stream the payload into staging and verify its size and SHA-256 before installation."""
    response = client.get_object(Bucket=bucket, Key=key)
    with closing(response["Body"]) as body:
        if response["ContentLength"] != size:
            raise ValueError(f"payload length differs from manifest: {key}")
        with path.open("wb") as output:
            if hash_stream(body, output) != (digest, size):
                raise ValueError(f"payload length or SHA-256 differs from manifest: {key}")
    return size
