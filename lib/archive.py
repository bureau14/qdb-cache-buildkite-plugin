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
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        raise tarfile.TarError(f"refusing unsafe tar member path: {name}")
    if posixpath.isabs(normalized):
        raise tarfile.TarError(f"refusing absolute tar member path: {name}")

    out = out.resolve()
    target = (out / normalized).resolve()
    try:
        target.relative_to(out)
    except ValueError as exc:
        raise tarfile.TarError(f"refusing tar member outside output dir: {name}") from exc
    return target


def _safe_tar_member(member, out):
    """Apply Python 3.12-style data filtering without the 3.12-only
    TarFile.extract(..., filter=...) keyword.

    On Python versions with tarfile.data_filter, delegate to the stdlib
    implementation. Otherwise reject traversal/absolute paths directly.
    Only regular files and directories are supported.
    """
    if not (member.isdir() or member.isreg()):
        raise tarfile.TarError(f"unsupported tar entry: {member.name!r}")
    _assert_tar_path_is_safe(out, member.name)
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is not None:
        return data_filter(member, str(out))
    return member


def validate_tar(tar_path: Path, roots: tuple[CacheRoot, ...], staging: Path):
    expected = {root.archive_name for root in roots}
    members = {}
    with tarfile.open(tar_path, "r:") as tar:
        for member in tar:
            if ".." in Path(member.name).parts:
                raise tarfile.TarError(f"refusing unsafe tar member path: {member.name!r}")
            if "/".join(member.name.split("/")[:2]) not in expected:
                raise tarfile.TarError(f"unexpected archive root: {member.name!r}")
            _safe_tar_member(member, staging)
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
    if len(parts) == 2:
        return root.destination
    _assert_tar_path_is_safe(root.destination, parts[2])
    return root.destination / parts[2]


def _metadata(path: Path, member: tarfile.TarInfo) -> None:
    os.chmod(path, member.mode & 0o777)
    os.utime(path, (member.mtime, member.mtime))


def merge_staged(staging: Path, roots: tuple[CacheRoot, ...], members) -> None:
    entries = [(name, member, _live_path(name, roots)) for name, member in sorted(members.items())]
    for _, member, path in entries:
        if path.is_symlink():
            raise tarfile.TarError(f"refusing destination symlink: {str(path)!r}")
        if path.exists() and path.is_dir() != member.isdir():
            raise tarfile.TarError(f"file/directory conflict: {str(path)!r}")
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
            _metadata(temporary, member)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    for _, member, path in reversed(entries):
        if member.isdir():
            _metadata(path, member)
