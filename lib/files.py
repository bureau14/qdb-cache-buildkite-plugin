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
    CacheRootError,
    check_root_spellings,
    name_key,
    normalize_path,
    reserved_windows_component,
)

BLOCK = 1024 * 1024


def layout(config: CacheConfig) -> dict:
    header = {
        "paths": [p.as_posix() for p in config.root_paths],
        "exclude": list(config.exclusions),
    }
    if config.external_layout:
        header["root_specs"] = list(config.external_layout)
    return header


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
    """Walk the available roots under their exclusions and validate the combined selection."""
    _check_mounts(config)
    entries = []
    for wire, root in config.root_paths.items():
        try:
            root_info = _inspect(root)
        except _Symlink:
            continue
        if root_info is None:
            continue

        pending = [(root, root_info)]
        while pending:
            source, info = pending.pop()
            if stat.S_ISLNK(info.st_mode):
                continue
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            if _excluded(source.relative_to(root), root, kind, config.exclusions):
                continue
            relative = wire / source.relative_to(root)
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
    roots = tuple(map(normalize_path, recorded["paths"]))
    check_root_spellings(roots)
    if {p.as_posix() for p in roots} != set(recorded["paths"]):
        raise ValueError("invalid cache manifest root spelling")
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
    if path.as_posix() != raw or (
        os.name == "nt" and any(reserved_windows_component(p) for p in path.parts)
    ):
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
    local_root = roots[root.as_posix()]
    if _excluded(relative, local_root, kind, patterns) or any(
        _excluded(p, local_root, "directory", patterns) for p in relative.parents
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


def _sibling_spellings(paths, stop_at=()) -> dict[str, dict[str, str]]:
    """Index each component, including implicit parents of standalone files."""
    children = {}
    for path in paths:
        while path != path.parent and path not in stop_at:
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
    entries = {}
    payloads = {}
    directories = set()
    roots = {p.as_posix(): local for p, local in config.root_paths.items()}
    resolved = {}
    for entry in records:
        root, path = _entry_path(entry, roots, config.exclusions)
        spelling = path.as_posix()
        if spelling in entries:
            raise ValueError(f"duplicate cache entry: {spelling!r}")
        entries[spelling] = entry
        local = roots[root.as_posix()] / path.relative_to(root)
        resolved[str(local)] = entry
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
    return resolved


def _check_destination_names(config: CacheConfig, paths) -> None:
    """Compare each requested spelling with the actual directory listing before staging."""
    # The agent supplies both bases. Resolving a home cache requires traversal
    # permission on its ancestors, but listing other users' homes is unnecessary.
    bases = (config.working_dir, Path.home())
    for parent_name, expected in _sibling_spellings(paths, stop_at=bases).items():
        parent = Path(parent_name)
        try:
            current = set(os.listdir(parent))
        except FileNotFoundError:
            continue
        for name in current:
            wanted = expected.get(name_key(name))
            if wanted is not None and wanted != name:
                # An external ancestor may have a distinct case-sensitive sibling
                # outside the selected trees. Its exact configured spelling wins.
                if wanted in current and not (
                    parent.is_relative_to(config.working_dir)
                    or any(parent.is_relative_to(root) for root in config.root_paths.values())
                ):
                    continue
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
    resolved = _validated_entries(manifest, config)
    if resolved is None:
        return None
    _check_mounts(config)
    for name, entry in resolved.items():
        _validate_destination(Path(name), entry["type"])
    _check_destination_names(config, map(Path, resolved))
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


def _linux_mount_points() -> dict:
    # mountinfo escapes whitespace and backslashes. Decode with filesystem
    # encoding so paths with arbitrary POSIX filename bytes compare correctly.
    def decode(field):
        raw = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), field)
        return Path(os.fsdecode(raw))

    with Path("/proc/self/mountinfo").open("rb") as table:
        points = {}
        for line in table:
            _, _, device, root, point, _ = line.split(maxsplit=5)
            points[decode(point)] = device, decode(root)
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


def _check_mount_aliases(config: CacheConfig, mounts: dict, clearing: bool) -> None:
    """Compare filesystem coordinates so a bind mount cannot alias a protected tree."""
    deepest_first = sorted(mounts, key=lambda p: len(p.parts), reverse=True)

    def location(path):
        for point in deepest_first:
            relative = _mount_relative(path, point)
            if relative is not None:
                device, source = mounts[point]
                return device, source / relative, path
        raise ValueError(f"cannot locate cache filesystem for {str(path)!r}")

    def within(child, ancestor):
        device, path, actual = child
        disk, parent, visible_parent = ancestor
        if device != disk:
            return False
        if path.is_relative_to(parent):
            return True
        depth = len(parent.parts)
        if name_key(str(Path(*path.parts[:depth]))) != name_key(str(parent)):
            return False
        # A difference in case can identify separate directories on Linux. Compare
        # actual inodes through both mount views, including the nearest existing
        # ancestor when a job has yet to create the configured cache.
        suffix = path.parts[depth:]
        other = visible_parent.joinpath(*suffix)
        for _ in range(len(suffix) + 1):
            try:
                return actual.samefile(other)
            except (FileNotFoundError, NotADirectoryError):
                actual, other = actual.parent, other.parent
        return False

    disjoint = [
        location(config.state_dir.parent.resolve()),
        location(config.state_dir.parent.parent),
    ]
    protected = [location(config.working_dir), location(Path.home().resolve())] if clearing else []
    for local in config.root_paths.values():
        root = location(local)
        if any(within(root, p) or within(p, root) for p in disjoint):
            raise CacheRootError(f"cache path aliases plugin files or another root: {str(local)!r}")
        if any(within(p, root) for p in protected):
            raise CacheRootError(
                f"clearing this cache root would include the checkout or home: {str(local)!r}"
            )
        disjoint.append(root)


def _check_native_mount(path: Path, *, allow_volume=False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    if (not allow_volume and os.path.ismount(path)) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"cache selection crosses mount or reparse point: {str(path)!r}")
    return info


def _check_mounts(config: CacheConfig, *, clearing=False) -> None:
    """Check mounts both on the approach to a root and anywhere inside its tree."""
    checkout = config.working_dir
    if sys.platform == "linux":
        # Bind mounts can have the same device number as the checkout.
        mounts = _linux_mount_points()
        _check_mount_aliases(config, mounts, clearing)
        for selected in config.root_paths.values():
            for point in mounts:
                if (point == checkout and selected.is_relative_to(checkout)) or not point.name:
                    continue
                inside = _mount_relative(point, selected) is not None
                below_checkout = _mount_relative(point, checkout) is not None
                if inside or (below_checkout and _mount_relative(selected, point) is not None):
                    raise ValueError(f"cache selection crosses mount: {str(point)!r}")
        return

    # Native mount/reparse checks require a walk even on restore: a mount may
    # exist in an unrelated subtree that has no entries in the saved manifest.
    for root in config.root_paths.values():
        point = checkout if root.is_relative_to(checkout) else Path(root.anchor)
        external = not root.is_relative_to(checkout)
        for part in root.relative_to(point).parts:
            point /= part
            if _check_native_mount(point, allow_volume=external and point != root) is None:
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


def _stale_entries(config: CacheConfig, entries: dict[str, dict]) -> list[tuple[Path, str, Path]]:
    """Inspect all roots before deleting anything and retain ancestors of exclusions and links."""
    _check_mounts(config, clearing=True)
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
            if stat.S_ISLNK(info.st_mode) or _excluded(
                path.relative_to(root), root, kind, config.exclusions
            ):
                keep.add(path.parent)
                continue
            candidates.append((path, _kind(info, path), root))
            if directory:
                # Leave the configured directory in place even when the cache is cold.
                if path == root:
                    keep.add(root)
                pending.extend((child, child.lstat()) for child in path.iterdir())

    # On a cold miss there are no manifest entries to trigger destination-name checks.
    _check_destination_names(config, config.root_paths.values())
    _sibling_spellings((p for p, _, _ in candidates), stop_at=(config.working_dir, Path.home()))
    stale = []
    for path, kind, root in reversed(candidates):
        if path in keep:
            keep.add(path.parent)
        else:
            stale.append((path, kind, root))
    return stale


def _remove_stale(path: Path, kind: str, root: Path) -> None:
    """Remove one checked entry, borrowing write permission only inside its cache root."""
    _validate_destination(path, kind)
    parent = path.parent
    mode = None
    if path != root and not os.access(parent, os.W_OK | os.X_OK):
        mode = stat.S_IMODE(parent.stat().st_mode)
        parent.chmod(mode | 0o300)
    try:
        if kind == "directory":
            path.rmdir()
        else:
            with _writable_for_removal(path):
                path.unlink()
    finally:
        if mode is not None:
            parent.chmod(mode)


def install(config: CacheConfig, entries: dict[str, dict], staged: dict[str, Path]) -> None:
    """Clear stale entries, install verified files, then restore directory modes."""
    if config.clear:
        for path, kind, root in _stale_entries(config, entries):
            _remove_stale(path, kind, root)
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
