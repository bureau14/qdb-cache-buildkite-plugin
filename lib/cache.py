#!/usr/bin/env python3
"""Cache restore/save and hook entry point."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import files
import object_store
from plugin_config import (
    CACHE_FORMAT_VERSION,
    CacheConfig,
    CacheRootError,
    load_config,
    validate_local_roots,
)


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


def _payload_key(prefix, generation, digest):
    return f"{prefix}/objects/{generation}/{digest}"


def _stage_files(client, bucket, prefix, destinations, tmp_dir, on_bytes):
    """Prepare private replacements concurrently against the one resolved manifest."""

    def prepare(item):
        index, (path, entry) = item
        staged = tmp_dir / str(index)
        if files.stage_local(Path(path), entry, staged):
            return path, staged if staged.exists() else None
        digest = entry["sha256"]
        object_store.download_payload(
            client,
            bucket,
            _payload_key(prefix, entry["generation"], digest),
            staged,
            entry["size"],
            digest,
            on_bytes=on_bytes,
        )
        return path, staged

    file_entries = (
        (path, entry) for path, entry in destinations.items() if entry["type"] == "file"
    )
    with ThreadPoolExecutor(max_workers=object_store.MAX_WORKERS) as pool:
        prepared = pool.map(prepare, enumerate(file_entries))
        return {path: staged for path, staged in prepared if staged is not None}


def _prepare_candidate(config, client, bucket, prefix, ref, staging, on_bytes):
    """Read the candidate once so every staged file comes from the same publication."""
    candidate = config.cache_prefix(prefix, ref)
    manifest, _ = object_store.get_manifest(client, bucket, f"{candidate}/manifest.json")
    if manifest is None:
        log(f"missing ref={ref or 'shared'!r}")
        return None
    destinations = files.destinations(config, manifest)
    if destinations is None:
        log(f"incompatible ref={ref or 'shared'!r}")
        return None
    replacements = _stage_files(client, bucket, candidate, destinations, staging, on_bytes)
    return destinations, replacements


def restore(config: CacheConfig, tmp_dir: Path) -> dict:
    """Stage each candidate, install the first complete hit, or clear after a clean miss."""
    validate_local_roots(config, clearing=config.clear)
    try:
        client, bucket, prefix = object_store.open_store(config)
    except ValueError as exc:
        return {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}

    result = {"outcome": "miss", "errors": [], "bytes": 0}
    count_lock = Lock()

    def count_bytes(size):
        # Parallel streams, including failed candidates, contribute to the hook total.
        with count_lock:
            result["bytes"] += size

    try:
        for ref in config.fallback_refs:
            ref_label = ref or "shared"
            with tempfile.TemporaryDirectory(dir=tmp_dir) as staging:
                try:
                    prepared = _prepare_candidate(
                        config, client, bucket, prefix, ref, Path(staging), count_bytes
                    )
                    if prepared is None:
                        continue
                    destinations, replacements = prepared
                except CacheRootError:
                    # Every fallback ref uses the same configured local destinations.
                    raise
                except Exception as exc:
                    result["errors"].append(f"ref={ref_label!r}: {exc}")
                    result["outcome"] = "error"
                    if config.strict:
                        return result
                    continue
                try:
                    files.install(config, destinations, replacements)
                except CacheRootError:
                    raise
                except Exception as exc:
                    # Fallback is safe only before installation starts changing live files.
                    result["errors"].append(f"install ref={ref_label!r}: {exc}")
                    result["outcome"] = "error"
                    return result
                log(f"restored source-ref={ref_label!r} target-ref={config.git_ref or 'shared'!r}")
                result["outcome"] = "restored"
                return result

        # A failed read may hide a valid snapshot. Clear on a miss only when
        # every candidate was confirmed missing or incompatible.
        if config.clear and not result["errors"]:
            try:
                files.install(config, {}, {})
            except CacheRootError:
                raise
            except Exception as exc:
                result["errors"].append(f"clear after miss: {exc}")
                result["outcome"] = "error"
        return result
    finally:
        client.close()


def _store_payloads(client, bucket, prefix, entries, sources, previous_entries, tmp_dir):
    """Reuse committed objects and finish all new uploads before the manifest can be published."""
    generations = {
        (e["sha256"], e["size"]): e["generation"] for e in previous_entries if e["type"] == "file"
    }
    # Separate writers can upload concurrently without replacing bytes held by an active reader.
    generation = uuid.uuid4().hex
    pending = set()
    for entry in entries:
        if entry["type"] == "file":
            digest = entry["sha256"]
            entry["generation"] = generations.get((digest, entry["size"]), generation)
            if entry["generation"] == generation:
                pending.add(digest)

    def upload(digest):
        path, size = sources[digest]
        key = _payload_key(prefix, generation, digest)
        return object_store.upload_payload(client, bucket, key, path, size, digest, tmp_dir)

    with ThreadPoolExecutor(max_workers=object_store.MAX_WORKERS) as pool:
        return sum(pool.map(upload, pending))


def save(config: CacheConfig, tmp_dir: Path) -> dict:
    """Select local inputs, reuse compatible payloads, and publish after all uploads succeed."""
    validate_local_roots(config)
    selection = files.select_inputs(config)
    if not selection:
        return {"outcome": "skipped: missing inputs or no payload", "bytes": 0}
    try:
        client, bucket, prefix = object_store.open_store(config)
    except ValueError as exc:
        return {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}
    try:
        target = config.cache_prefix(prefix)
        key = f"{target}/manifest.json"
        previous, etag = object_store.get_manifest(
            client, bucket, key, tolerate_invalid=config.force
        )
        previous_entries = []
        try:
            if previous is not None and files.validate_manifest(previous, config):
                previous_entries = previous["entries"]
        except ValueError:
            # Without trusted references, a forced save uploads every digest from local inputs.
            if not config.force:
                raise
        entries, sources = files.capture(selection)
        transferred = _store_payloads(
            client, bucket, target, entries, sources, previous_entries, tmp_dir
        )
        if not config.force and previous_entries == entries:
            return {"outcome": "unchanged", "bytes": transferred}
        manifest = {
            "format": CACHE_FORMAT_VERSION,
            "layout": files.layout(config),
            "entries": entries,
        }
        if not object_store.publish_manifest(client, bucket, key, manifest, etag):
            return {"outcome": "skipped: concurrent manifest change", "bytes": transferred}
        return {"outcome": "saved", "bytes": transferred}
    finally:
        client.close()


def run(operation: str) -> int:
    """Validate configuration, dispatch operations, and report its result."""
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
    except CacheRootError as exc:
        result = {"outcome": "configuration error", "fatal": True, "errors": [str(exc)]}
    except Exception as exc:
        result = {"outcome": "error", "errors": [str(exc)]}
    errors = result.get("errors", [])
    fatal = bool(result.get("fatal") or (errors and config.strict))
    log(
        f"{operation} outcome={result['outcome']!r} identity={config.identity!r} "
        f"target-ref={config.git_ref or 'shared'!r} bytes={result.get('bytes', 'unknown')} "
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
