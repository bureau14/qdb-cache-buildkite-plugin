"""Select files, directories and links and install one validated manifest into local cache roots."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from plugin_config import CACHE_FORMAT_VERSION, CacheConfig, normalize_path

BLOCK = 1024 * 1024


def layout(config: CacheConfig) -> dict:
    header = {
        "paths": [p.as_posix() for p in config.root_paths],
        "exclude": list(config.exclusions),
    }
    if config.external_layout:
        header["root_specs"] = list(config.external_layout)
    return header


def _inspect(path: Path) -> os.stat_result | None:
    """Inspect the leaf itself, allowing traversal only through ordinary directories."""
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return None
        if component == path:
            return info
        if _kind(info, component) != "directory":
            raise NotADirectoryError(f"cache path parent is not a directory: {str(component)!r}")


def open_regular(path: Path):
    info = _inspect(path)
    if info is None:
        raise FileNotFoundError(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"expected a regular file: {str(path)!r}")
    return path.open("rb")


def hash_stream(data, copy=None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: data.read(BLOCK), b""):
        digest.update(chunk)
        size += len(chunk)
        if copy is not None:
            copy.write(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class SourceEntry:
    relative: Path
    source: Path
    kind: str
    info: os.stat_result


def _kind(info: os.stat_result, path: Path) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    # A Windows junction also redirects directory traversal.
    if os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
        raise ValueError(f"unsupported cache junction: {str(path)!r}")
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    raise ValueError(f"unsupported cache file type: {str(path)!r}")


def _excluded(relative: Path, root: Path, kind: str, patterns: tuple[str, ...]) -> bool:
    pattern = relative.as_posix() if relative.parts else (root.name if kind != "directory" else "")
    return bool(pattern) and any(fnmatch.fnmatchcase(pattern, p) for p in patterns)


def select_inputs(config: CacheConfig) -> list[SourceEntry]:
    """Walk included directories and sort by manifest path for stable hard-link targets."""
    entries = []
    for wire, root in config.root_paths.items():
        root_info = _inspect(root)
        if root_info is None:
            continue

        pending = [(root, root_info)]
        while pending:
            source, info = pending.pop()
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            if _excluded(source.relative_to(root), root, kind, config.exclusions):
                continue
            relative = wire / source.relative_to(root)
            _wire_path(relative.as_posix())
            kind = _kind(info, source)
            entries.append(SourceEntry(relative, source, kind, info))
            if kind == "directory":
                pending.extend((child, child.lstat()) for child in source.iterdir())
    return sorted(entries, key=lambda item: item.relative.as_posix())


def capture(selection: list[SourceEntry]) -> tuple[list[dict], dict[str, tuple[Path, int]]]:
    """Record link targets and hash the first selected name of each regular file."""
    entries = []
    payloads = {}
    hardlinks = {}
    for item in selection:
        path = item.relative.as_posix()
        entry = {"path": path, "type": item.kind, "mode": item.info.st_mode & 0o777}
        if item.kind == "symlink":
            entry["target"] = os.readlink(item.source)
            # Windows needs the link type even when its target is missing.
            if getattr(item.info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY:
                entry["directory"] = True
        elif item.kind == "file":
            if item.info.st_nlink > 1 and item.info.st_ino:
                inode = item.info.st_dev, item.info.st_ino
                target = hardlinks.setdefault(inode, path)
                if target != path:
                    entry.update(type="hardlink", target=target)
                    entries.append(entry)
                    continue
            with open_regular(item.source) as data:
                digest, size = hash_stream(data)
            entry.update(sha256=digest, size=size)
            payloads.setdefault(digest, (item.source, size))
        entries.append(entry)
    return entries, payloads


def _validated_entries(manifest: dict, config: CacheConfig) -> dict | None:
    """Check compatibility before interpreting entries belonging to another snapshot layout."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("format"), str):
        raise ValueError("invalid cache manifest format")
    if manifest["format"] != CACHE_FORMAT_VERSION:
        return None
    recorded = manifest.get("layout")
    if (
        not isinstance(recorded, dict)
        or not isinstance(recorded.get("paths"), list)
        or not recorded["paths"]
        or not all(isinstance(p, str) for p in recorded["paths"])
        or not isinstance(recorded.get("exclude"), list)
        or not all(isinstance(p, str) for p in recorded["exclude"])
    ):
        raise ValueError("invalid cache manifest layout")
    if "root_specs" in recorded:
        specs = recorded["root_specs"]
        if (
            not isinstance(specs, list)
            or not all(isinstance(s, str) and s for s in specs)
            or specs != sorted(set(specs))
            or recorded["paths"] != [str(i) for i in range(len(specs))]
        ):
            raise ValueError("invalid cache manifest root specifications")
    if recorded != layout(config):
        return None
    records = manifest.get("entries")
    if not isinstance(records, list) or not records:
        raise ValueError("invalid cache manifest entries")
    return _manifest_entries(records, config)


def validate_manifest(manifest: dict, config: CacheConfig) -> bool:
    """Only a structurally valid, matching manifest can supply payloads for the next save."""
    return _validated_entries(manifest, config) is not None


def _wire_path(raw: str) -> Path:
    if not isinstance(raw, str):
        raise ValueError("invalid cache manifest path")
    path = normalize_path(raw)
    if path.as_posix() != raw:
        raise ValueError(f"unsafe cache manifest path: {raw!r}")
    return path


def _entry_path(
    entry: dict, roots: dict[str, Path], patterns: tuple[str, ...]
) -> tuple[Path, Path]:
    """Validate file metadata, then locate the owning root to enforce its exclusions."""
    if not isinstance(entry, dict):
        raise ValueError("invalid cache manifest entry")
    raw = entry.get("path")
    path = _wire_path(raw)
    kind = entry.get("type")
    mode = entry.get("mode")
    if (
        kind not in ("file", "directory", "symlink", "hardlink")
        or type(mode) is not int
        or not 0 <= mode <= 0o777
    ):
        raise ValueError(f"invalid cache manifest type or mode for {raw!r}")
    if kind != "directory" and not path.parts:
        raise ValueError("cache manifest describes the checkout itself as a file")
    if kind == "file" and not (
        type(entry.get("size")) is int
        and entry["size"] >= 0
        and isinstance(entry.get("sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
        and isinstance(entry.get("generation"), str)
        and re.fullmatch(r"[a-f0-9]{32}", entry["generation"])
    ):
        raise ValueError(f"invalid cache file metadata for {raw!r}")
    if kind in ("symlink", "hardlink"):
        target = entry.get("target")
        if not isinstance(target, str) or not target or "\0" in target:
            raise ValueError(f"invalid cache link target for {raw!r}")
        if kind == "symlink" and not isinstance(entry.get("directory", False), bool):
            raise ValueError(f"invalid symlink directory flag for {raw!r}")
    root = next((p for p in (path, *path.parents) if p.as_posix() in roots), None)
    if root is None:
        raise ValueError(f"cache manifest entry outside configured paths: {raw!r}")
    relative = path.relative_to(root)
    local_root = roots[root.as_posix()]
    if _excluded(relative, local_root, kind, patterns) or any(
        _excluded(p, local_root, "directory", patterns) for p in relative.parents
    ):
        raise ValueError(f"excluded cache manifest entry: {raw!r}")
    return root, path


def _validate_destination(path: Path, kind: str) -> None:
    info = _inspect(path)
    if info is not None and (_kind(info, path) == "directory") != (kind == "directory"):
        raise ValueError(f"incompatible local cache file type: {str(path)!r}")


def _manifest_entries(records: list, config: CacheConfig) -> dict[str, dict]:
    """Validate unique names, payload references and the directory hierarchy of each root."""
    locations = {}
    payloads = {}
    directories = set()
    roots = {p.as_posix(): local for p, local in config.root_paths.items()}
    resolved = {}
    for entry in records:
        root, path = _entry_path(entry, roots, config.exclusions)
        spelling = path.as_posix()
        if spelling in locations:
            raise ValueError(f"duplicate cache entry: {spelling!r}")
        local = str(roots[root.as_posix()] / path.relative_to(root))
        locations[spelling] = local
        resolved[local] = entry
        if entry["type"] == "file":
            reference = entry["size"], entry["generation"]
            if payloads.setdefault(entry["sha256"], reference) != reference:
                raise ValueError(f"conflicting cache payload references for {spelling!r}")
        # Ancestors above a standalone root are implicit. Inside it the writer
        # records each directory, including its permission bits.
        while path.as_posix() != root.as_posix():
            path = path.parent
            directories.add(path.as_posix())
    if any(resolved.get(locations.get(d), {}).get("type") != "directory" for d in directories):
        raise ValueError("cache manifest is missing a required directory entry")
    for local, entry in resolved.items():
        if entry["type"] == "hardlink":
            # A hard link can only reference an included regular file, never an arbitrary host path.
            target = locations.get(entry["target"])
            source = resolved.get(target, {})
            if source.get("type") != "file" or source["mode"] != entry["mode"]:
                raise ValueError(f"invalid cache hard link for {entry['path']!r}")
            resolved[local] = {**entry, "target": target}
    return resolved


def destinations(config: CacheConfig, manifest: dict) -> dict[str, dict] | None:
    """Resolve a compatible snapshot and check all live destinations before downloads."""
    resolved = _validated_entries(manifest, config)
    if resolved is None:
        return None
    for name, entry in resolved.items():
        _validate_destination(Path(name), entry["type"])
    return resolved


def _mode_matches(current: int, requested: int) -> bool:
    # Windows chmod represents only the read-only attribute through the owner write bit.
    if os.name == "nt":
        return (current & stat.S_IWRITE) == (requested & stat.S_IWRITE)
    return stat.S_IMODE(current) == requested


def stage_local(path: Path, entry: dict, staged: Path) -> bool:
    """Verify local bytes and stage a private copy when permissions or links require it."""
    expected = entry["sha256"], entry["size"]
    try:
        info = _inspect(path)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
            return False
        with path.open("rb") as data:
            if info.st_nlink == 1 and _mode_matches(info.st_mode, entry["mode"]):
                return hash_stream(data) == expected
            # Detach existing hard links so restoring modes or links cannot affect outside names.
            with staged.open("wb") as copy:
                return hash_stream(data, copy) == expected
    except OSError:
        # A download overwrites any incomplete staged copy.
        return False


def _replace(source: Path, path: Path, mode: int) -> None:
    # A sibling temp makes the final replacement atomic across staging filesystems.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".qdb-cache-", delete=False) as data:
        temporary = Path(data.name)
    try:
        try:
            os.replace(source, temporary)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copyfile(source, temporary)
        os.chmod(temporary, mode & 0o777)
        _validate_destination(path, "file")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stale_entries(config: CacheConfig, entries: dict[str, dict]) -> list[tuple[Path, str]]:
    """Inspect roots without following links, retaining the parents of every excluded entry."""
    candidates = []
    keep = set(map(Path, entries))
    for root in config.root_paths.values():
        root_info = _inspect(root)
        if root_info is None:
            continue
        pending = [(root, root_info)]
        while pending:
            path, info = pending.pop()
            directory = stat.S_ISDIR(info.st_mode)
            kind = "directory" if directory else "file"
            if _excluded(path.relative_to(root), root, kind, config.exclusions):
                keep.add(path.parent)
                continue
            kind = _kind(info, path)
            candidates.append((path, kind))
            if directory:
                # Leave the configured directory in place even when the cache is cold.
                if path == root:
                    keep.add(root)
                pending.extend((child, child.lstat()) for child in path.iterdir())

    stale = []
    for path, kind in reversed(candidates):
        if path in keep:
            keep.add(path.parent)
        else:
            stale.append((path, kind))
    return stale


def _install_link(path: Path, entry: dict) -> None:
    # Prepare beside the destination so a failed link creation preserves the old entry.
    _validate_destination(path, entry["type"])
    if entry["type"] == "symlink" and path.is_symlink() and os.readlink(path) == entry["target"]:
        if os.name != "nt" or bool(
            path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY
        ) == entry.get("directory", False):
            return
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".qdb-cache-") as tmp:
        link = Path(tmp) / "link"
        if entry["type"] == "symlink":
            link.symlink_to(entry["target"], target_is_directory=entry.get("directory", False))
        else:
            source = Path(entry["target"])
            info = _inspect(source)
            if info is None or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"missing regular file for hard link: {str(source)!r}")
            os.link(source, link)
        os.replace(link, path)


def install(config: CacheConfig, entries: dict[str, dict], staged: dict[str, Path]) -> None:
    """Clear stale paths, install verified files before their links, then apply directory modes."""
    if config.clear:
        for path, kind in _stale_entries(config, entries):
            _validate_destination(path, kind)
            if kind == "directory":
                path.rmdir()
            else:
                path.unlink()
    directories = []
    links = []
    for name, entry in sorted(entries.items()):
        path = Path(name)
        kind = entry["type"]
        _validate_destination(path, kind)
        if kind == "directory":
            path.mkdir(parents=True, exist_ok=True)
            directories.append((path, entry["mode"]))
        elif kind in ("hardlink", "symlink"):
            links.append((path, entry))
        elif name in staged:
            path.parent.mkdir(parents=True, exist_ok=True)
            _replace(staged[name], path, entry["mode"])
    for path, entry in links:
        path.parent.mkdir(parents=True, exist_ok=True)
        _install_link(path, entry)
    for path, mode in reversed(directories):
        if not _mode_matches(path.stat().st_mode, mode):
            path.chmod(mode)
