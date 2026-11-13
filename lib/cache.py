#!/usr/bin/env python3
"""Cache restore/save and hook entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import zstandard

import archive
import object_store
from plugin_config import FORMAT, CacheConfig, load_config


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


def restore(config: CacheConfig, scratch: Path) -> dict:
    try:
        client, bucket, prefix = object_store.open_store(config)
    except ValueError as exc:
        return {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}
    events, errors = [], []
    transferred = 0
    try:
        for index, ref in enumerate(config.fallback_refs):
            key = config.object_key(prefix, ref)
            candidate = scratch / f"candidate-{index}"
            candidate.mkdir()
            response = None
            try:
                response = object_store.get_snapshot(client, bucket, key)
                if response is None:
                    events.append(f"missing ref={ref!r}")
                    continue
                metadata = response.get("Metadata", {})
                if (
                    metadata.get("qdb-cache-format") != FORMAT
                    or metadata.get("qdb-cache-layout-sha256") != config.layout_digest
                ):
                    events.append(f"incompatible ref={ref!r}")
                    continue
                digest = metadata.get("qdb-cache-sha256", "")
                compressed = candidate / "snapshot.tar.zst"
                with compressed.open("wb") as output:
                    for chunk in iter(lambda: response["Body"].read(archive.BLOCK), b""):
                        output.write(chunk)
                        transferred += len(chunk)
                tar_path = candidate / "snapshot.tar"
                with compressed.open("rb") as data, tar_path.open("wb") as output:
                    with zstandard.ZstdDecompressor().stream_reader(data) as reader:
                        shutil.copyfileobj(reader, output, archive.BLOCK)
                if archive.sha256_file(tar_path) != digest:
                    raise tarfile.TarError("snapshot checksum mismatch")
                staging = candidate / "staging"
                members = archive.validate_tar(tar_path, config.roots, staging)
                archive.extract_staged(tar_path, staging, members)
            except Exception as exc:
                errors.append(f"ref={ref!r}: {exc}")
                if config.strict:
                    return {
                        "outcome": "error",
                        "errors": errors,
                        "events": events,
                        "bytes": transferred,
                    }
                continue
            finally:
                if response is not None:
                    response["Body"].close()
            try:
                archive.merge_staged(staging, config.roots, members)
            except Exception as exc:
                # Another candidate cannot safely repair a partially completed merge.
                errors.append(f"merge ref={ref!r}: {exc}")
                return {
                    "outcome": "error",
                    "errors": errors,
                    "events": events,
                    "bytes": transferred,
                }
            events.append(f"restored source-ref={ref!r} target-ref={config.git_ref!r}")
            return {"outcome": "restored", "errors": errors, "events": events, "bytes": transferred}
        return {"outcome": "miss", "errors": errors, "events": events, "bytes": transferred}
    finally:
        client.close()


def save(config: CacheConfig, scratch: Path) -> dict:
    tar_path = scratch / "snapshot.tar"
    digest = archive.create_tar(config.roots, config.exclusions, tar_path)
    if digest is None:
        return {"outcome": "skipped: missing inputs or no payload", "bytes": 0}
    metadata = {
        "qdb-cache-format": FORMAT,
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
        compressed = scratch / "snapshot.tar.zst"
        with tar_path.open("rb") as data, compressed.open("wb") as output:
            zstandard.ZstdCompressor().copy_stream(data, output)
        # Each identity owns one replaceable snapshot; concurrent saves use backend ordering.
        object_store.upload_snapshot(client, bucket, key, compressed, metadata)
        return {"outcome": "saved", "bytes": compressed.stat().st_size}
    finally:
        client.close()


def run(operation: str) -> int:
    try:
        config = load_config()
    except ValueError as exc:
        log(f"configuration error: {exc}")
        annotate(operation, "configuration", [str(exc)], fatal=True)
        return 1
    if operation == "validate":
        log("configuration valid")
        return 0
    if not {"restore": config.restore, "save": config.save}[operation]:
        log(f"{operation} disabled; identity={config.identity!r} bytes=0")
        return 0
    if operation == "save" and os.environ.get("BUILDKITE_COMMAND_EXIT_STATUS") != "0":
        log(f"save skipped: command success was not explicit; identity={config.identity!r} bytes=0")
        return 0
    started = time.monotonic()
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{operation}-", dir=config.state_dir) as directory:
            result = {"restore": restore, "save": save}[operation](config, Path(directory))
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
    parser.add_argument("operation", choices=("restore", "save", "validate"))
    args = parser.parse_args(argv)
    return run(args.operation)


if __name__ == "__main__":
    sys.exit(main())
