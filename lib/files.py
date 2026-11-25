"""Select files, directories and links and install one validated manifest into local cache roots."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from plugin_config import CacheConfig, normalize_path

BLOCK = 1024 * 1024


def layout(config: CacheConfig) -> dict:
    header = {
        "paths": [p.as_posix() for p in config.root_paths],
        "exclude": list(config.exclusions),
    }
    if config.external_layout:
        header["root_specs"] = list(config.external_layout)
    return header


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_parents(paths) -> None:
    # Inspect shared ancestors once, parent first, so lstat never traverses an unchecked link.
    for parent in sorted({parent for path in paths for parent in path.parents}):
        info = _lstat(parent)
        if info is not None and _kind(info, parent) != "directory":
            raise NotADirectoryError(f"cache path parent is not a directory: {str(parent)!r}")


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


def _walk_root(root: Path, patterns: tuple[str, ...]):
    """Yield entries before their children. Excluded paths have no kind and are pruned."""
    root_info = _lstat(root)
    if root_info is None:
        return
    pending = [(root, root_info)]
    while pending:
        path, info = pending.pop()
        directory = stat.S_ISDIR(info.st_mode)
        if _excluded(path.relative_to(root), root, "directory" if directory else "file", patterns):
            yield path, info, None
            continue
        kind = _kind(info, path)
        yield path, info, kind
        if kind == "directory":
            pending.extend((child, child.lstat()) for child in path.iterdir())


def select_inputs(config: CacheConfig) -> list[SourceEntry]:
    """Sort included entries by manifest path for stable hard-link targets."""
    entries = []
    _check_parents(config.root_paths.values())
    for wire, root in config.root_paths.items():
        for path, info, kind in _walk_root(root, config.exclusions):
            if kind is not None:
                relative = normalize_path((wire / path.relative_to(root)).as_posix())
                entries.append(SourceEntry(relative, path, kind, info))
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
            with item.source.open("rb") as data:
                digest, size = hash_stream(data)
            entry.update(sha256=digest, size=size)
            payloads.setdefault(digest, (item.source, size))
        entries.append(entry)
    return entries, payloads


def compatible_entries(manifest: dict, config: CacheConfig) -> list | None:
    """Check compatibility before interpreting entries belonging to another snapshot layout."""
    recorded = manifest.get("layout")
    exclusions = recorded.get("exclude") if isinstance(recorded, dict) else None
    if not isinstance(exclusions, list) or not all(isinstance(p, str) for p in exclusions):
        raise ValueError("invalid cache manifest exclusions")
    # Paths determine the storage key. Only exclusions can differ at the same key.
    expected = layout(config)
    if recorded != {**expected, "exclude": exclusions}:
        raise ValueError("invalid cache manifest layout")
    if exclusions != expected["exclude"]:
        return None
    # An empty hit would otherwise clear eligible local files without restoring anything.
    if not manifest["entries"]:
        raise ValueError("empty cache manifest")
    return manifest["entries"]


def _entry_path(
    entry: dict, roots: dict[str, Path], patterns: tuple[str, ...]
) -> tuple[Path, Path]:
    """Locate the owning root and enforce the configured restore scope."""
    raw = entry["path"]
    path = normalize_path(raw)
    kind = entry["type"]
    root = next((p for p in (path, *path.parents) if p.as_posix() in roots), None)
    if root is None:
        raise ValueError(f"cache manifest entry outside configured paths: {raw!r}")
    relative = path.relative_to(root)
    local_root = roots[root.as_posix()]
    # Every directory inside a root must have its own entry, so its exclusion is checked there.
    if _excluded(relative, local_root, kind, patterns):
        raise ValueError(f"excluded cache manifest entry: {raw!r}")
    return root, path


def _manifest_entries(records: list, config: CacheConfig) -> dict[str, dict]:
    """Validate unique names, directory parents and hard-link destinations."""
    locations = {}
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
        # Each recorded parent requires its own parent, up to the configured root.
        if path != root:
            directories.add(path.parent.as_posix())
    if any(resolved.get(locations.get(d), {}).get("type") != "directory" for d in directories):
        raise ValueError("cache manifest is missing a required directory entry")
    for local, entry in resolved.items():
        if entry["type"] == "hardlink":
            # A hard link can only reference an included regular file, never an arbitrary host path.
            target = locations.get(entry["target"])
            if resolved.get(target, {}).get("type") != "file":
                raise ValueError(f"invalid cache hard link for {entry['path']!r}")
            resolved[local] = {**entry, "target": target}
    return resolved


def destinations(config: CacheConfig, manifest: dict) -> dict[str, dict] | None:
    """Resolve a compatible snapshot and check all live destinations before downloads."""
    records = compatible_entries(manifest, config)
    if records is None:
        return None
    resolved = _manifest_entries(records, config)
    # Every parent inside a cache root has a directory record. Check those records
    # before their children; staging and installation then use this job-owned tree.
    roots = config.root_paths.values()
    _check_parents(root for root in roots if config.clear or str(root) in resolved)
    for path in sorted(map(Path, resolved)):
        info = _lstat(path)
        expects_directory = resolved[str(path)]["type"] == "directory"
        if info is not None and (_kind(info, path) == "directory") != expects_directory:
            raise ValueError(f"incompatible local cache file type: {str(path)!r}")
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
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stale_entries(config: CacheConfig, entries: dict[str, dict]) -> list[tuple[Path, str]]:
    """Inspect roots without following links, retaining the parents of every excluded entry."""
    candidates = []
    keep = set(map(Path, entries))
    for root in config.root_paths.values():
        for path, _, kind in _walk_root(root, config.exclusions):
            if kind is None:
                keep.add(path.parent)
                continue
            candidates.append((path, kind))
            # Leave the configured directory in place even when the cache is cold.
            if path == root and kind == "directory":
                keep.add(root)

    stale = []
    for path, kind in reversed(candidates):
        if path in keep:
            keep.add(path.parent)
        else:
            stale.append((path, kind))
    return stale


def _install_link(path: Path, entry: dict) -> None:
    # Prepare beside the destination so a failed link creation preserves the old entry.
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
            os.link(entry["target"], link)
        os.replace(link, path)


def install(config: CacheConfig, entries: dict[str, dict], staged: dict[str, Path]) -> None:
    """Clear stale paths, install verified files before their links, then apply directory modes."""
    if config.clear:
        if not entries:
            # A clean miss has no manifest preflight before clearing the configured roots.
            _check_parents(config.root_paths.values())
        for path, kind in _stale_entries(config, entries):
            if kind == "directory":
                path.rmdir()
            else:
                path.unlink()
    directories = []
    links = []
    for name, entry in sorted(entries.items()):
        path = Path(name)
        kind = entry["type"]
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
            path.chmod(mode & 0o777)
