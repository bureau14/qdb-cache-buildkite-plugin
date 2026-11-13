"""Inherited ARTIFACTS configuration and S3/R2 data access."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from plugin_config import CacheConfig

BACKEND_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/backend"
DESTINATION_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/destination"
ENDPOINT_URL_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/endpoint-url"
R2_ACCOUNT_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/account-id"
R2_ACCESS_KEY_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/access-key-id"
R2_SECRET_ACCESS_KEY_SSM_PARAM = "/services/buildkite/credentials/artifacts/r2/secret-access-key"
ARTIFACTS_DOMAIN_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/artifacts-domain"


@dataclass(frozen=True)
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    artifacts_domain: str | None = None


@dataclass(frozen=True)
class ObjectAuth:
    """Per-operation S3 credentials. All None for S3 (IAM role handles auth).
    For R2, populated from SSM-stored credentials derived from the Cloudflare API token.
    """

    access_key_id: str | None = None
    secret_access_key: str | None = None


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
    """Die if value looks like a placeholder that was never replaced with a real one."""
    if value and any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        die(
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
        die(
            f"unsupported artifact backend: {backend!r} (expected 's3' or 'r2'). "
            f"Check env var ARTIFACTS_BACKEND or SSM parameter {BACKEND_SSM_PARAM}"
        )

    destination = os.environ.get("ARTIFACTS_DESTINATION") or _ssm_get_optional(
        ssm, DESTINATION_SSM_PARAM
    )
    if not destination:
        die(
            f"artifact destination not configured. "
            f"Set env var ARTIFACTS_DESTINATION or SSM parameter {DESTINATION_SSM_PARAM}"
        )

    # Applies to both backends; mainly for local MinIO / dev setups.
    endpoint_url = os.environ.get("ARTIFACTS_ENDPOINT_URL")

    artifacts_domain = os.environ.get("ARTIFACTS_DOMAIN") or _ssm_get_optional(
        ssm, ARTIFACTS_DOMAIN_SSM_PARAM
    )

    if backend == "r2":
        endpoint_url = endpoint_url or _ssm_get_optional(ssm, ENDPOINT_URL_SSM_PARAM)

        r2_account_id = _env_or_ssm(ssm, "ARTIFACTS_R2_ACCOUNT_ID", R2_ACCOUNT_ID_SSM_PARAM)
        if not r2_account_id:
            die(
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
            die(
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
            die(
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
            r2_account_id=r2_account_id,
            r2_access_key_id=r2_access_key_id,
            r2_secret_access_key=r2_secret_access_key,
            artifacts_domain=artifacts_domain,
        )

    return StoreConfig(
        backend=backend,
        destination=destination,
        endpoint_url=endpoint_url,
        artifacts_domain=artifacts_domain,
    )


def parse_s3(uri):
    """Parse s3://bucket/prefix URI into (bucket, prefix).
    Also handles plain bucket names (legacy SSM format)."""
    if not uri.startswith("s3://"):
        return uri.strip("/"), ""
    p = urlparse(uri)
    return p.netloc, p.path.strip("/")


def key_join(*parts):
    """Join S3 key segments, dropping blanks to avoid double slashes."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def resolve_object_auth(ssm, cfg, permission):
    """S3 → empty auth (IAM role chain). R2 → use stored S3-compatible credentials."""
    if cfg.backend == "r2":
        return ObjectAuth(
            access_key_id=cfg.r2_access_key_id,
            secret_access_key=cfg.r2_secret_access_key,
        )
    return ObjectAuth()


def _s3_client(cfg, auth, operation: CacheConfig):
    """Create an S3-compatible client using the artifacts plugin's backend settings.

    R2 needs signature_version=s3v4 and addressing_style=path (R2 endpoints are
    account-level, not bucket-level — virtual-host style would produce invalid hostnames).
    """
    client_cfg = sdk_config(operation)
    if cfg.backend == "r2":
        client_cfg = sdk_config(
            operation, signature_version="s3v4", s3={"addressing_style": "path"}
        )

    kwargs = {"config": client_cfg}
    if cfg.backend == "r2":
        kwargs["region_name"] = "auto"
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    if auth.access_key_id:
        kwargs["aws_access_key_id"] = auth.access_key_id
        kwargs["aws_secret_access_key"] = auth.secret_access_key

    return boto3.client("s3", **kwargs)


def die(msg):
    raise ValueError(msg)


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


def _is_client_config_error(exc: Exception) -> bool:
    # Botocore validation exceptions lack a shared base class. Constructor-only
    # Invalid/Config families are deterministic setup errors; credential/transport
    # failures are re-raised for the operational policy.
    return isinstance(exc, (ValueError, ParamValidationError)) or any(
        base.__name__.startswith("Invalid")
        or "Config" in base.__name__
        or base.__name__ == "ProfileNotFound"
        for base in type(exc).__mro__
    )


def open_store(operation: CacheConfig):
    try:
        ssm = aws_clients(operation)
    except (ValueError, BotoCoreError) as exc:
        if not _is_client_config_error(exc):
            raise
        raise ValueError("invalid AWS configuration for the SSM client") from exc
    try:
        cfg = load_store_config(ssm)
        if cfg.endpoint_url:
            try:
                endpoint = urlparse(cfg.endpoint_url)
                valid = (
                    endpoint.scheme in {"http", "https"}
                    and endpoint.hostname
                    and endpoint.port != 0
                    and not endpoint.username
                    and not endpoint.password
                    and not endpoint.query
                    and not endpoint.fragment
                    and "\\" not in cfg.endpoint_url
                    and not any(c.isspace() or ord(c) < 32 for c in cfg.endpoint_url)
                )
            except ValueError as exc:
                raise ValueError("ARTIFACTS_ENDPOINT_URL must be a valid HTTP(S) endpoint") from exc
            if not valid:
                raise ValueError("ARTIFACTS_ENDPOINT_URL must be a valid HTTP(S) endpoint")
        try:
            bucket, prefix = parse_s3(cfg.destination)
            parsed = urlparse(cfg.destination)
        except ValueError as exc:
            raise ValueError("ARTIFACTS_DESTINATION contains an invalid URI") from exc
        # Match SDK bucket-parameter syntax while preserving legacy SSM bucket
        # names; backend-specific naming and authorization remain backend-owned.
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", bucket):
            raise ValueError("ARTIFACTS_DESTINATION must be s3://bucket/prefix or a bucket name")
        if cfg.destination.startswith("s3://") and (parsed.query or parsed.fragment):
            raise ValueError("ARTIFACTS_DESTINATION cannot contain a query or fragment")
        try:
            client = _s3_client(cfg, resolve_object_auth(ssm, cfg, "read-write"), operation)
        except (ValueError, BotoCoreError) as exc:
            if not _is_client_config_error(exc):
                raise
            # SDK endpoint/region validation happens during client construction,
            # before requests; malformed configuration must remain fatal.
            raise ValueError("invalid resolved storage endpoint or client configuration") from exc
        return client, bucket, prefix
    finally:
        ssm.close()


def is_missing(exc: ClientError) -> bool:
    # Authorization failures (including ambiguous 403s) must never become misses.
    return str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "NotFound", "404"}


def head_snapshot(client, bucket: str, key: str):
    try:
        return client.head_object(Bucket=bucket, Key=key).get("Metadata", {})
    except ClientError as exc:
        if is_missing(exc):
            return None
        raise


def get_snapshot(client, bucket: str, key: str):
    try:
        return client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if is_missing(exc):
            return None
        raise


def upload_snapshot(client, bucket: str, key: str, path: Path, metadata: dict[str, str]):
    client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ContentType": "application/zstd", "Metadata": metadata},
        Config=TransferConfig(),
    )
