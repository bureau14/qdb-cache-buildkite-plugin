"""Select plain files and install one validated manifest into local cache roots."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from plugin_config import (
    CACHE_FORMAT_VERSION,
    CacheConfig,
    name_key,
    normalize_path,
    normalize_roots,
    reserved_windows_component,
)

BLOCK = 1024 * 1024


def layout(config: CacheConfig) -> dict:
    return {"paths": [p.as_posix() for p in config.roots], "exclude": list(config.exclusions)}


class _Symlink(ValueError):
    pass


def _inspect(path: Path) -> os.stat_result | None:
    """Check every existing component before allowing a read or replacement."""
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise _Symlink(f"symlinked cache path: {str(component)!r}")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"cache path parent is not a directory: {str(component)!r}")
    return info


@contextmanager
def open_regular(path: Path):
    """Recheck the source and opened descriptor because selection precedes transfer."""
    info = _inspect(path)
    if info is None:
        raise FileNotFoundError(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"expected a regular file: {str(path)!r}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as data:
        if _kind(os.fstat(data.fileno()), path) != "file":
            raise ValueError(f"expected a regular file: {str(path)!r}")
        yield data


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
    mode: int


def _kind(info: os.stat_result, path: Path) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        # Changing permissions or build outputs through a shared inode affects its other names.
        if info.st_nlink != 1:
            raise ValueError(f"cache file must have exactly one hard link: {str(path)!r}")
        return "file"
    raise ValueError(f"unsupported cache file type: {str(path)!r}")


def _excluded(relative: Path, root: Path, kind: str, patterns: tuple[str, ...]) -> bool:
    pattern = relative.as_posix() if relative.parts else (root.name if kind != "directory" else "")
    return bool(pattern) and any(fnmatch.fnmatchcase(pattern, p) for p in patterns)


def select_inputs(config: CacheConfig) -> list[SourceEntry]:
    """Check required roots first, then walk each selection under its own exclusions."""
    _check_mounts(config)
    selected = []
    for relative in config.roots:
        root = config.working_dir / relative
        try:
            info = _inspect(root)
        except _Symlink:
            continue
        except NotADirectoryError:
            return []
        if info is None:
            return []
        selected.append((root, info))

    entries = []
    for root, root_info in selected:
        pending = [(root, root_info)]
        while pending:
            source, info = pending.pop()
            if stat.S_ISLNK(info.st_mode):
                continue
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            if _excluded(source.relative_to(root), root, kind, config.exclusions):
                continue
            relative = source.relative_to(config.working_dir)
            _wire_path(relative.as_posix())
            entries.append(SourceEntry(relative, source, _kind(info, source), info.st_mode & 0o777))
            if kind == "directory":
                pending.extend((child, child.lstat()) for child in source.iterdir())
    _sibling_spellings(item.relative for item in entries)
    return sorted(entries, key=lambda item: item.relative.as_posix())


def capture(selection: list[SourceEntry]) -> tuple[list[dict], dict[str, tuple[Path, int]]]:
    """Hash the selected files and keep one source for each potentially missing payload."""
    entries = []
    payloads = {}
    for item in selection:
        entry = {"path": item.relative.as_posix(), "type": item.kind, "mode": item.mode}
        if item.kind == "file":
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
    roots = normalize_roots(tuple(recorded["paths"]))
    if {p.as_posix() for p in roots} != set(recorded["paths"]):
        raise ValueError("invalid cache manifest root spelling")
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
    if path.as_posix() != raw or (
        os.name == "nt" and any(reserved_windows_component(p) for p in path.parts)
    ):
        raise ValueError(f"unsafe cache manifest path: {raw!r}")
    return path


def _entry_path(entry: dict, roots: set[str], patterns: tuple[str, ...]) -> tuple[Path, Path]:
    """Validate file metadata, then locate the owning root to enforce its exclusions."""
    if not isinstance(entry, dict):
        raise ValueError("invalid cache manifest entry")
    raw = entry.get("path")
    path = _wire_path(raw)
    kind = entry.get("type")
    mode = entry.get("mode")
    if kind not in ("file", "directory") or type(mode) is not int or not 0 <= mode <= 0o777:
        raise ValueError(f"invalid cache manifest type or mode for {raw!r}")
    if kind == "file" and not path.parts:
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
    root = next((p for p in (path, *path.parents) if p.as_posix() in roots), None)
    if root is None:
        raise ValueError(f"cache manifest entry outside configured paths: {raw!r}")
    relative = path.relative_to(root)
    if _excluded(relative, root, kind, patterns) or any(
        _excluded(p, root, "directory", patterns) for p in relative.parents
    ):
        raise ValueError(f"excluded cache manifest entry: {raw!r}")
    return root, path


@contextmanager
def _writable_for_removal(path: Path):
    mode = None
    if sys.platform == "win32":
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
                and _kind(info, path) == "file"
            ):
                mode = stat.S_IMODE(info.st_mode)
    if mode is not None:
        path.chmod(mode | stat.S_IWRITE)
    try:
        yield
    except BaseException:
        if mode is not None:
            path.chmod(mode)
        raise


def _validate_destination(path: Path, kind: str) -> None:
    info = _inspect(path)
    if info is not None and _kind(info, path) != kind:
        raise ValueError(f"incompatible local cache file type: {str(path)!r}")


def _sibling_spellings(paths) -> dict[str, dict[str, str]]:
    """Index each component, including implicit parents of standalone files."""
    children = {}
    for path in paths:
        while path.parts:
            siblings = children.setdefault(path.parent.as_posix(), {})
            prior = siblings.setdefault(name_key(path.name), path.name)
            if prior != path.name:
                raise ValueError(
                    f"conflicting cache names in {str(path.parent)!r}: {prior!r}, {path.name!r}"
                )
            path = path.parent
    return children


def _manifest_entries(records: list, config: CacheConfig) -> dict[str, dict]:
    """Validate unique names, payload references and the directory hierarchy of each root."""
    roots = {p.as_posix() for p in config.roots}
    entries = {}
    payloads = {}
    directories = set()
    for entry in records:
        root, path = _entry_path(entry, roots, config.exclusions)
        spelling = path.as_posix()
        if spelling in entries:
            raise ValueError(f"duplicate cache entry: {spelling!r}")
        entries[spelling] = entry
        if entry["type"] == "file":
            reference = entry["size"], entry["generation"]
            if payloads.setdefault(entry["sha256"], reference) != reference:
                raise ValueError(f"conflicting cache payload references for {spelling!r}")
        # Ancestors above a standalone root are implicit. Inside it the writer
        # records each directory, including its permission bits.
        while path.as_posix() != root.as_posix():
            path = path.parent
            directories.add(path.as_posix())
    if any(entries.get(d, {}).get("type") != "directory" for d in directories):
        raise ValueError("cache manifest is missing a required directory entry")
    _sibling_spellings(map(Path, entries))
    return entries


def _check_destination_names(checkout: Path, paths) -> None:
    """Compare each requested spelling with the actual directory listing before staging."""
    for relative, expected in _sibling_spellings(paths).items():
        parent = checkout / relative
        try:
            current = set(os.listdir(parent))
        except FileNotFoundError:
            continue
        for name in current:
            wanted = expected.get(name_key(name))
            if wanted is not None and wanted != name:
                raise ValueError(
                    f"conflicting local cache name in {str(parent)!r}: {name!r}, {wanted!r}"
                )
        # Some filesystems support aliases beyond Unicode folding (e.g. DOS short names).
        for name in set(expected.values()) - current:
            if os.path.lexists(parent / name):
                raise ValueError(
                    f"cache path aliases a different local name: {str(parent / name)!r}"
                )


def destinations(config: CacheConfig, manifest: dict) -> dict[str, dict] | None:
    """Resolve a compatible snapshot and check all live destinations before downloads."""
    entries = _validated_entries(manifest, config)
    if entries is None:
        return None
    _check_mounts(config)
    resolved = {str(config.working_dir / name): entry for name, entry in entries.items()}
    for spelling, entry in resolved.items():
        _validate_destination(Path(spelling), entry["type"])
    _check_destination_names(config.working_dir, map(Path, entries))
    return resolved


def _mode_matches(current: int, requested: int) -> bool:
    # Windows chmod represents only the read-only attribute through the owner write bit.
    if os.name == "nt":
        return (current & stat.S_IWRITE) == (requested & stat.S_IWRITE)
    return stat.S_IMODE(current) == requested


def stage_local(path: Path, entry: dict, staged: Path) -> bool:
    """Verify local bytes and stage a private replacement when permissions differ."""
    expected = entry["sha256"], entry["size"]
    try:
        with open_regular(path) as data:
            info = os.fstat(data.fileno())
            if info.st_size != entry["size"]:
                return False
            if _mode_matches(info.st_mode, entry["mode"]):
                return hash_stream(data) == expected
            # A writable parent permits replacement even when the job cannot chmod this file.
            with staged.open("wb") as copy:
                return hash_stream(data, copy) == expected
    except OSError:
        # A download overwrites any incomplete staged copy.
        return False


def _linux_mount_points() -> set[Path]:
    # mountinfo escapes whitespace and backslashes. Decode with filesystem
    # encoding so paths with arbitrary POSIX filename bytes compare correctly.
    with Path("/proc/self/mountinfo").open("rb") as table:
        points = set()
        for line in table:
            field = line.split(maxsplit=5)[4]
            unescaped = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), field)
            points.add(Path(os.fsdecode(unescaped)))
        return points


def _mount_relative(path: Path, parent: Path) -> Path | None:
    if path.is_relative_to(parent):
        return path.relative_to(parent)
    depth = len(parent.parts)
    if len(path.parts) < depth:
        return None
    prefix = Path(*path.parts[:depth])
    # A Linux casefold volume can resolve a configured spelling to the same
    # mount even though mountinfo retains its original spelling.
    if name_key(str(prefix)) == name_key(str(parent)):
        try:
            if prefix.samefile(parent):
                return Path(*path.parts[depth:])
        except (FileNotFoundError, NotADirectoryError):
            pass
    return None


def _check_native_mount(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    if os.path.ismount(path) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"cache selection crosses mount or reparse point: {str(path)!r}")
    return info


def _check_mounts(config: CacheConfig) -> None:
    """Check mounts both on the approach to a root and anywhere inside its tree."""
    checkout = config.working_dir
    if sys.platform == "linux":
        # Bind mounts can have the same device number as the checkout.
        for point in _linux_mount_points():
            relative = _mount_relative(point, checkout)
            if relative is None or not relative.parts:
                continue
            for root in config.roots:
                selected = checkout / root
                if (
                    _mount_relative(point, selected) is not None
                    or _mount_relative(selected, point) is not None
                ):
                    raise ValueError(f"cache selection crosses mount: {str(point)!r}")
        return

    # Native mount/reparse checks require a walk even on restore: a mount may
    # exist in an unrelated subtree that has no entries in the saved manifest.
    for root in config.roots:
        point = checkout
        for part in root.parts:
            point /= part
            if _check_native_mount(point) is None:
                break
        else:
            if point.is_dir():
                pending = [point]
                while pending:
                    for child in pending.pop().iterdir():
                        info = _check_native_mount(child)
                        if info is not None and stat.S_ISDIR(info.st_mode):
                            pending.append(child)


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
        with _writable_for_removal(path):
            os.replace(temporary, path)
    finally:
        with _writable_for_removal(temporary):
            temporary.unlink(missing_ok=True)


def install(entries: dict[str, dict], staged: dict[str, Path]) -> None:
    """Prepare the target, install verified files, and apply directory modes after their children."""
    # Existing snapshot directories may be read-only. Temporarily permit child
    # writes only after staging succeeds and only in the affected directory trees.
    creates = set(staged)
    creates.update(
        name
        for name, entry in entries.items()
        if entry["type"] == "directory" and not Path(name).exists()
    )
    parents = {str(ancestor) for name in creates for ancestor in Path(name).parents}
    directories = []
    try:
        for name, entry in sorted(entries.items()):
            path = Path(name)
            kind = entry["type"]
            _validate_destination(path, kind)
            if kind == "directory":
                path.mkdir(parents=True, exist_ok=True)
                directories.append((path, entry["mode"]))
                if name in parents and not os.access(path, os.W_OK | os.X_OK):
                    os.chmod(path, stat.S_IMODE(path.stat().st_mode) | 0o300)
            elif name in staged:
                path.parent.mkdir(parents=True, exist_ok=True)
                _replace(staged[name], path, entry["mode"])
    finally:
        # Close any temporary directory permissions even when installation stops partway.
        for path, mode in reversed(directories):
            _validate_destination(path, "directory")
            if not _mode_matches(path.stat().st_mode, mode):
                os.chmod(path, mode & 0o777)
