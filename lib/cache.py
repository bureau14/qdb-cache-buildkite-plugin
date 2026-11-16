#!/usr/bin/env python3
"""Cache restore/save and hook entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import zstandard

import archive
import object_store
from plugin_config import CACHE_FORMAT_VERSION, CacheConfig, load_config


def log(message: str) -> None:
    print(f"[qdb-cache] {message}", file=sys.stderr, flush=True)


def annotate(operation: str, identity: str, messages: list[str], fatal: bool) -> None:
    if not os.environ.get("BUILDKITE"):
        return
    body = f"qdb-cache {operation} error for {identity!r}:\n\n" + "\n\n".join(messages)
    executable = "buildkite-agent.exe" if os.name == "nt" else "buildkite-agent"
    try:
        subprocess.run(
            [
                executable,
                "annotate",
                "--context",
                f"qdb-cache-{operation}",
                "--style",
                "error" if fatal else "warning",
                "--scope",
                "job",
            ],
            input=body,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"annotation reporting failed ({type(exc).__name__}); original result retained")


def _stage_snapshot(config: CacheConfig, response: dict, tmp_dir: Path, result: dict):
    # Prepare a snapshot before any live files change:
    # 1. Download and decompress into tmp_dir.
    # 2. Validate the archive layout.
    # 3. Extract validated members into staging.
    metadata = response.get("Metadata", {})
    if (
        metadata.get("qdb-cache-format") != CACHE_FORMAT_VERSION
        or metadata.get("qdb-cache-layout-sha256") != config.layout_digest
    ):
        return None
    compressed = tmp_dir / "snapshot.tar.zst"
    with compressed.open("wb") as output:
        for chunk in iter(lambda: response["Body"].read(archive.BLOCK), b""):
            output.write(chunk)
            # Failed candidates still contribute to the transferred-byte total.
            result["bytes"] += len(chunk)
    tar_path = tmp_dir / "snapshot.tar"
    with compressed.open("rb") as data, tar_path.open("wb") as output:
        with zstandard.ZstdDecompressor().stream_reader(data) as reader:
            shutil.copyfileobj(reader, output, archive.BLOCK)
    staging = tmp_dir / "staging"
    members = archive.validate_tar(tar_path, config.roots, staging)
    archive.extract_staged(tar_path, staging, members)
    return staging, members


def restore(config: CacheConfig, tmp_dir: Path) -> dict:
    try:
        client, bucket, prefix = object_store.open_store(config)
    except ValueError as exc:
        return {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}
    result = {"outcome": "miss", "errors": [], "events": [], "bytes": 0}
    try:
        for index, ref in enumerate(config.fallback_refs):
            key = config.object_key(prefix, ref)
            candidate_tmp_dir = tmp_dir / f"candidate-{index}"
            candidate_tmp_dir.mkdir()
            response = None
            try:
                response = object_store.get_snapshot(client, bucket, key)
                if response is None:
                    result["events"].append(f"missing ref={ref!r}")
                    continue
                prepared = _stage_snapshot(config, response, candidate_tmp_dir, result)
                if prepared is None:
                    result["events"].append(f"incompatible ref={ref!r}")
                    continue
            except Exception as exc:
                result["errors"].append(f"ref={ref!r}: {exc}")
                if config.strict:
                    result["outcome"] = "error"
                    return result
                continue
            finally:
                if response is not None:
                    response["Body"].close()
            staging, members = prepared
            try:
                archive.merge_staged(staging, config.roots, members)
            except Exception as exc:
                # Installing cached files preserves unrelated live files but can stop
                # partway; another candidate cannot safely repair a partial merge.
                result["errors"].append(f"merge ref={ref!r}: {exc}")
                result["outcome"] = "error"
                return result
            result["events"].append(f"restored source-ref={ref!r} target-ref={config.git_ref!r}")
            result["outcome"] = "restored"
            return result
        return result
    finally:
        client.close()


def save(config: CacheConfig, tmp_dir: Path) -> dict:
    # This function is responsible for publishing a snapshot. It works by:
    # 1. Capturing inputs and their checksum in the caller-owned temporary directory.
    # 2. Comparing metadata to avoid unnecessary compression and uploads.
    # 3. Compressing and uploading changed content to the current ref.
    tar_path = tmp_dir / "snapshot.tar"
    digest = archive.create_tar(config.roots, config.exclusions, tar_path)
    if digest is None:
        return {"outcome": "skipped: missing inputs or no payload", "bytes": 0}
    # Format/layout gate restore compatibility; the digest identifies unchanged
    # snapshots so saves can skip compression/upload.
    metadata = {
        "qdb-cache-format": CACHE_FORMAT_VERSION,
        "qdb-cache-layout-sha256": config.layout_digest,
        "qdb-cache-sha256": digest,
    }
    try:
        client, bucket, prefix = object_store.open_store(config)
    except ValueError as exc:
        return {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}
    try:
        key = config.object_key(prefix)
        existing = object_store.head_snapshot(client, bucket, key)
        if (
            not config.force
            and existing is not None
            and all(existing.get(name) == value for name, value in metadata.items())
        ):
            return {"outcome": "unchanged", "bytes": 0}
        compressed = tmp_dir / "snapshot.tar.zst"
        with tar_path.open("rb") as data, compressed.open("wb") as output:
            zstandard.ZstdCompressor().copy_stream(data, output)
        # Each identity owns one replaceable snapshot; concurrent saves use backend ordering.
        object_store.upload_snapshot(client, bucket, key, compressed, metadata)
        return {"outcome": "saved", "bytes": compressed.stat().st_size}
    finally:
        client.close()


def run(operation: str) -> int:
    # This function is responsible for applying hook failure policy. It works by:
    # 1. Validating configuration before any storage access.
    # 2. Skipping disabled operations and saves without command success.
    # 3. Dispatching the operation with automatically cleaned temporary scratch.
    # 4. Reporting errors, failing configuration errors and strict-mode operations.
    try:
        config = load_config()
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        log(f"configuration error: {exc}")
        annotate(operation, "configuration", [str(exc)], fatal=True)
        return 1
    handler, enabled = {
        "restore": (restore, config.restore),
        "save": (save, config.save),
    }[operation]
    if not enabled:
        log(f"{operation} disabled; identity={config.identity!r} bytes=0")
        return 0
    if operation == "save" and os.environ.get("BUILDKITE_COMMAND_EXIT_STATUS") != "0":
        log(f"save skipped: command success was not explicit; identity={config.identity!r} bytes=0")
        return 0
    started = time.monotonic()
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"{operation}-", dir=config.state_dir
        ) as tmp_directory:
            result = handler(config, Path(tmp_directory))
    except Exception as exc:
        result = {"outcome": "error", "errors": [str(exc)]}
    errors = result.get("errors", [])
    fatal = bool(result.get("fatal") or (errors and config.strict))
    for event in result.get("events", []):
        log(event)
    log(
        f"{operation} outcome={result['outcome']!r} identity={config.identity!r} "
        f"target-ref={config.git_ref!r} bytes={result.get('bytes', 'unknown')} "
        f"duration={time.monotonic() - started:.3f}s"
    )
    if errors:
        for error in errors:
            log(error)
        annotate(operation, config.identity, errors, fatal)
    return 1 if fatal else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Restore or save a Buildkite cache")
    parser.add_argument("operation", choices=("restore", "save"))
    args = parser.parse_args(argv)
    return run(args.operation)


if __name__ == "__main__":
    sys.exit(main())
