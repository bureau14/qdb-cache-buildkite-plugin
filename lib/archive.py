"""Tar snapshots and staged merge for regular files and directories."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import tarfile
import tempfile
from functools import partial
from pathlib import Path

import zstandard

BLOCK = 1024 * 1024


def _include_member(member, root: Path, working_dir: Path, exclusions: tuple[str, ...]):
    relative = Path(member.name).relative_to(root)
    path = working_dir / member.name
    pattern = relative.as_posix() if relative.parts else (root.name if not member.isdir() else "")
    if pattern and any(fnmatch.fnmatchcase(pattern, p) for p in exclusions):
        return None
    if path.is_symlink() or not (member.isreg() or member.isdir()):
        raise tarfile.TarError(f"unsupported input type: {str(path)!r}")
    # Ownership varies between agents and must not change the checksum.
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    # Touching files or recreating directories must not change the cache checksum.
    member.mtime = 0
    member.mode &= 0o777
    return member


def create_tar(
    working_dir: Path, roots: tuple[Path, ...], exclusions: tuple[str, ...], output: Path
):
    """Capture working-directory paths with stable metadata for checksum comparison."""
    if any(not (working_dir / root).exists() for root in roots):
        return None
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT, dereference=True) as tar:
        for root in roots:
            tar.add(
                working_dir / root,
                arcname=root.as_posix(),
                filter=partial(
                    _include_member, root=root, working_dir=working_dir, exclusions=exclusions
                ),
            )
        payload = any(member.isreg() for member in tar.members)
    return sha256_file(output) if payload else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(BLOCK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decompress_snapshot(compressed: Path, output: Path) -> None:
    decoder = zstandard.ZstdDecompressor().decompressobj()
    with compressed.open("rb") as data, output.open("wb") as destination:
        # The incremental API exposes frame completion but returns all output per
        # input chunk; small reads bound allocations for highly compressed caches.
        for chunk in iter(lambda: data.read(1024), b""):
            while chunk:
                if decoder.eof:
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                destination.write(decoder.decompress(chunk))
                chunk = decoder.unused_data
        if not decoder.eof:
            raise zstandard.ZstdError("incomplete compressed snapshot")


def validate_tar(tar_path: Path, roots: tuple[Path, ...]):
    """Accept regular files and directories within the configured relative paths."""
    members = {}
    with tarfile.open(tar_path, "r:") as tar:
        for member in tar:
            path = Path(member.name)
            if not member.name or path.anchor or ".." in path.parts:
                raise tarfile.TarError(f"unsafe tar member path: {member.name!r}")
            if not any(path.is_relative_to(root) for root in roots):
                raise tarfile.TarError(f"unexpected archive path: {member.name!r}")
            if not (member.isdir() or member.isreg()):
                raise tarfile.TarError(f"unsupported tar entry: {member.name!r}")
            members[member.name] = member
    if not members:
        raise tarfile.TarError("snapshot contains no roots")
    return members


def extract_staged(tar_path: Path, staging: Path, members: dict[str, tarfile.TarInfo]) -> None:
    # Keep staging writable; cached modes are applied at the live destinations.
    staging.mkdir()
    with tarfile.open(tar_path, "r:") as tar:
        for name, member in sorted(members.items()):
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            else:
                with tar.extractfile(member) as data, target.open("wb") as output:
                    shutil.copyfileobj(data, output, BLOCK)


def merge_staged(staging: Path, working_dir: Path, members) -> None:
    """Restore staged files to a clean checkout, applying directory modes last."""
    entries = sorted(members.items())
    for name, member in entries:
        path = working_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if member.isdir():
            path.mkdir(exist_ok=True)
            continue
        # A sibling replacement keeps failed copies from exposing a partial file.
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".qdb-cache-", delete=False
        ) as data:
            temporary = Path(data.name)
        try:
            shutil.copyfile(staging / name, temporary)
            # Temporary files start at 0600; retain cached executable/access bits.
            os.chmod(temporary, member.mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    # Apply directory modes last so read-only modes do not block child writes.
    for name, member in reversed(entries):
        if member.isdir():
            os.chmod(working_dir / name, member.mode & 0o777)
