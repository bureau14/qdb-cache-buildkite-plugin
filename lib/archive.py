"""Tar snapshots and staged merge for regular files and directories."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import shutil
import tarfile
import tempfile
from functools import partial
from pathlib import Path

from plugin_config import CacheRoot

BLOCK = 1024 * 1024


def _include_member(member, root: CacheRoot, exclusions: tuple[str, ...]):
    relative = member.name[len(root.archive_name) :].lstrip("/")
    path = root.destination / relative
    pattern = relative or (root.destination.name if not member.isdir() else "")
    if pattern and any(fnmatch.fnmatchcase(pattern, p) for p in exclusions):
        return None
    if path.is_symlink() or not (member.isreg() or member.isdir()):
        raise tarfile.TarError(f"unsupported input type: {str(path)!r}")
    # Ownership varies between agents and must not change the checksum.
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mode &= 0o777
    return member


def create_tar(roots: tuple[CacheRoot, ...], exclusions: tuple[str, ...], output: Path):
    if any(not root.destination.exists() for root in roots):
        return None
    payload = False
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT, dereference=True) as tar:
        for root in roots:
            start = len(tar.members)
            tar.add(
                root.destination,
                arcname=root.archive_name,
                filter=partial(_include_member, root=root, exclusions=exclusions),
            )
            for member in tar.members[start:]:
                relative = member.name[len(root.archive_name) :].lstrip("/")
                # Naturally empty directories are payload; fully excluded trees are not.
                payload = (
                    payload or member.isreg() or not any((root.destination / relative).iterdir())
                )
    return sha256_file(output) if payload else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(BLOCK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_tar_path_is_safe(out, name):
    if not Path(name).parts or ".." in Path(name).parts:
        raise tarfile.TarError(f"refusing unsafe tar member path: {name}")
    if posixpath.isabs(name):
        raise tarfile.TarError(f"refusing absolute tar member path: {name}")

    out = out.resolve()
    if not (out / name).resolve().is_relative_to(out):
        raise tarfile.TarError(f"refusing tar member outside output dir: {name}")


def validate_tar(tar_path: Path, roots: tuple[CacheRoot, ...], staging: Path):
    expected = {root.archive_name for root in roots}
    members = {}
    with tarfile.open(tar_path, "r:") as tar:
        for member in tar:
            if "/".join(member.name.split("/")[:2]) not in expected:
                raise tarfile.TarError(f"unexpected archive root: {member.name!r}")
            if not (member.isdir() or member.isreg()):
                raise tarfile.TarError(f"unsupported tar entry: {member.name!r}")
            # extractfile() supplies bytes; our manual writes need their own path boundary.
            _assert_tar_path_is_safe(staging, member.name)
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


def _live_path(name: str, roots: tuple[CacheRoot, ...]) -> Path:
    parts = name.split("/", 2)
    root = next(root for root in roots if root.archive_name == "/".join(parts[:2]))
    if root.destination.is_symlink():
        raise tarfile.TarError(f"refusing destination symlink: {str(root.destination)!r}")
    path = root.destination
    if len(parts) == 3:
        _assert_tar_path_is_safe(path, parts[2])
        path = path / parts[2]
    if path.is_symlink():
        raise tarfile.TarError(f"refusing destination symlink: {str(path)!r}")
    return path


def _restore_metadata(path: Path, mode: int, mtime: float) -> None:
    # Temporary-file replacement loses executable bits; changed mtimes also alter
    # the tar digest used to skip unchanged uploads.
    os.chmod(path, mode & 0o777)
    os.utime(path, (mtime, mtime))


def merge_staged(staging: Path, roots: tuple[CacheRoot, ...], members) -> None:
    entries = [(name, member, _live_path(name, roots)) for name, member in sorted(members.items())]
    for name, member, path in entries:
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
            _restore_metadata(temporary, member.mode, member.mtime)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    # Apply directory metadata last so read-only modes do not block child writes.
    for _, member, path in reversed(entries):
        if member.isdir():
            _restore_metadata(path, member.mode, member.mtime)
