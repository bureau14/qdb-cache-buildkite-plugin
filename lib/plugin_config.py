"""Buildkite cache configuration and job state."""

from __future__ import annotations

import math
import ntpath
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Mapping
from unicodedata import normalize
from urllib.parse import quote

PREFIX = "BUILDKITE_PLUGIN_QDB_CACHE_"
CACHE_FORMAT_VERSION = "files-v3"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CacheConfig:
    root_paths: dict[Path, Path]
    external_layout: tuple[str, ...]
    working_dir: Path
    pairs: dict[str, str]
    exclusions: tuple[str, ...]
    pipeline: str
    git_ref: str | None
    state_dir: Path
    restore: bool
    clear: bool
    save: bool
    fallback_refs: tuple[str | None, ...]
    force: bool
    strict: bool
    retries: int
    connect_timeout: float
    read_timeout: float

    @property
    def identity(self) -> str:
        parts = [self.pipeline]
        if self.git_ref is not None:
            parts.append(self.git_ref)
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(parts)

    def cache_prefix(self, prefix: str, git_ref: str | None = None) -> str:
        parts = [
            prefix.strip("/"),
            self.pipeline,
            (git_ref or self.git_ref) if self.git_ref is not None else None,
            "cache",
            CACHE_FORMAT_VERSION,
        ]
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(p for p in parts if p)


# Boolean/list parsing follows qdb-artifacts' _get_env_bool/_get_env_array helpers.
def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(PREFIX + name)
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _strings(env: Mapping[str, str], name: str, required: bool = False) -> tuple[str, ...]:
    prefix = PREFIX + name
    values = []
    while (value := env.get(f"{prefix}_{len(values)}")) is not None:
        if not value:
            raise ValueError(f"{name.lower()}: values must not be empty")
        values.append(value)
    if not values and env.get(prefix):
        values.append(env[prefix])
    if required and not values:
        raise ValueError(f"{name.lower()}: expected at least one value")
    return tuple(values)


def _pairs(env: Mapping[str, str]) -> dict[str, str]:
    pairs = {}
    index = 0
    while (key := env.get(f"{PREFIX}KEYS_{index}_KEY")) is not None:
        if not key:
            raise ValueError("keys: keys must not be empty")
        if key in pairs:
            raise ValueError("keys: duplicate keys")
        pairs[key] = env[f"{PREFIX}KEYS_{index}_VALUE"]
        index += 1
    return dict(sorted(pairs.items()))


def _positive_timeout(env: Mapping[str, str], name: str, default: float) -> float:
    message = f"{name.lower().replace('_', '-')} must be a positive finite number"
    try:
        value = float(env.get(PREFIX + name, str(default)))
    except ValueError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(message)
    return value


def _current_buildkite_git_ref(env: Mapping[str, str]) -> str | None:
    """Return the current Buildkite git ref in artifact namespace format, if known."""
    tag = env.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else f"refs/tags/{tag}"

    branch = env.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    return None


def name_key(name: str) -> str:
    return normalize("NFC", normalize("NFD", name).casefold())


def reserved_windows_component(name: str) -> bool:
    if hasattr(ntpath, "isreserved"):
        # Keep a colon in the component from being interpreted as a drive prefix.
        return ntpath.isreserved(".\\" + name)
    # Python 3.9-3.12 has the DOS device check in pathlib. The other forms
    # can name an alternate stream, collapse into another name, or fail at installation.
    return (
        name.endswith((" ", "."))
        or any(ord(c) < 32 or c in '<>:"|?*' for c in name)
        or PureWindowsPath(name.partition(".")[0].rstrip(" ")).is_reserved()
    )


def normalize_path(raw: str) -> Path:
    """Normalize relative coordinates without expanding a home, drive, or parent."""
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("path must be a nonempty checkout-relative name")
    path = Path(raw)
    if (
        path.anchor
        or (os.name != "nt" and "\\" in raw)
        or ".." in path.parts
        or (path.parts and path.parts[0].startswith("~"))
        or any(PureWindowsPath(part).drive for part in path.parts)
    ):
        raise ValueError(f"unsafe checkout-relative path: {raw!r}")
    return path


def check_root_spellings(roots) -> None:
    """Reject duplicate, overlapping, or ambiguous roots, including implicit parents."""
    spellings = {}
    names = set()
    for root in roots:
        name = root.as_posix()
        if name in names:
            raise ValueError(f"duplicate cache root: {name!r}")
        names.add(name)
        # Two roots can select different children through ambiguous parent names.
        for index, part in enumerate(root.parts):
            key = (*root.parts[:index], name_key(part))
            if spellings.setdefault(key, part) != part:
                raise ValueError(f"ambiguous cache root spelling: {name!r}")
    for root in roots:
        if any(parent.as_posix() in names for parent in root.parents):
            raise ValueError(f"overlapping cache root: {root.as_posix()!r}")


def _checkout(env: Mapping[str, str], cwd: Path | None = None) -> Path:
    return Path(env.get("BUILDKITE_BUILD_CHECKOUT_PATH") or cwd or Path.cwd()).resolve()


def _in_tree(path: Path, root: Path) -> bool:
    # Path comparison alone misses alternative spellings on case-insensitive POSIX volumes.
    for component in (path, *path.parents):
        if component == root:
            return True
        try:
            if component.samefile(root):
                return True
        except (FileNotFoundError, NotADirectoryError):
            continue
    return False


class CacheRootError(ValueError):
    """An invalid path configuration is fatal even for best-effort caches."""


def validate_state_dir(checkout: Path, state_dir: Path) -> None:
    """Keep every job's private state outside the checkout, including during setup and cleanup."""
    shared = state_dir.parent
    resolved = shared.resolve()
    if _in_tree(shared, checkout) or _in_tree(resolved, checkout) or _in_tree(checkout, resolved):
        raise CacheRootError("the checkout or cache root and shared plugin state must be disjoint")
    if state_dir.resolve() != resolved / state_dir.name:
        raise CacheRootError("job state directory must reside inside shared plugin state")


def _configured_roots(
    paths: tuple[str, ...], checkout: Path
) -> tuple[dict[Path, Path], tuple[str, ...]]:
    """Bind trusted pipeline paths to local roots and assign portable manifest names.

    A checkout-only layout retains its existing wire paths. Mixed layouts number
    the sorted path specifications, so a home-relative root maps to each job's home.
    """
    locations = {}
    external = False
    for raw in paths:
        path = Path(raw)
        if "\0" in raw or ".." in path.parts or (os.name != "nt" and "\\" in raw):
            raise ValueError(f"invalid cache path: {raw!r}")
        if path.is_absolute() or (path.parts and path.parts[0] == "~"):
            external = True
            local = path.expanduser()
        else:
            local = checkout / normalize_path(raw)
        if os.name == "nt" and any(
            reserved_windows_component(p) for p in local.parts if p != local.anchor
        ):
            raise ValueError(f"reserved Windows cache path: {raw!r}")
        if not local.name or path.as_posix() in locations:
            raise ValueError(f"filesystem root or duplicate cache path: {raw!r}")
        locations[path.as_posix()] = local

    specs = sorted(locations)
    # Validate the actual destination components too, including implicit parents
    # shared by a checkout-relative root and an absolute root.
    check_root_spellings(locations.values())
    if external:
        return {Path(str(i)): locations[s] for i, s in enumerate(specs)}, tuple(specs)
    return {Path(s): locations[s] for s in specs}, ()


def validate_local_roots(config: CacheConfig, *, clearing=False) -> None:
    """Check live aliases only for enabled operations so access failures follow strict mode."""
    plugin = config.state_dir.parent.parent
    checked = [plugin]
    for local in config.root_paths.values():
        validate_state_dir(local, config.state_dir)
        if any(_in_tree(local, p) or _in_tree(p, local) for p in checked):
            raise CacheRootError(
                f"cache root overlaps the plugin or another cache root: {str(local)!r}"
            )
        if clearing and (_in_tree(config.working_dir, local) or _in_tree(Path.home(), local)):
            raise CacheRootError(
                f"clearing this cache root would include the checkout or home: {str(local)!r}"
            )
        checked.append(local)


def load_config(
    env: Mapping[str, str] | None = None, cwd: Path | None = None, plugin_dir: Path = PLUGIN_DIR
) -> CacheConfig:
    """Load cache paths, identity, fallback refs, and SDK options from Buildkite."""
    env = os.environ if env is None else env
    checkout = _checkout(env, cwd)
    job_id = env.get("BUILDKITE_JOB_ID", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
        raise ValueError("invalid or missing BUILDKITE_JOB_ID")
    state_dir = plugin_dir / ".state" / job_id
    validate_state_dir(checkout, state_dir)
    pipeline = env["BUILDKITE_PIPELINE_SLUG"]
    use_git_ref = _boolean(env, "GIT_REF", True)
    git_ref = _current_buildkite_git_ref(env) if use_git_ref else None
    if use_git_ref and git_ref is None:
        raise ValueError("BUILDKITE_BRANCH or BUILDKITE_TAG is required")
    mode = env.get(PREFIX + "FALLBACK", "default")
    if mode not in {"default", "disabled"}:
        raise ValueError("fallback must be default or disabled")
    refs = [git_ref]
    if git_ref is not None and mode == "default":
        refs.extend(ref for ref in ("refs/heads/master", "refs/heads/main") if ref != git_ref)
    try:
        retries = int(env.get(PREFIX + "RETRIES", "3"))
    except ValueError as exc:
        raise ValueError("retries must be a positive integer") from exc
    if retries <= 0:
        raise ValueError("retries must be a positive integer")
    roots, external_layout = _configured_roots(_strings(env, "PATH", required=True), checkout)
    return CacheConfig(
        root_paths=roots,
        external_layout=external_layout,
        working_dir=checkout,
        pairs=_pairs(env),
        exclusions=tuple(sorted(set(_strings(env, "EXCLUDE")))),
        pipeline=pipeline,
        git_ref=git_ref,
        state_dir=state_dir,
        restore=_boolean(env, "RESTORE", True),
        clear=_boolean(env, "CLEAR", True),
        save=_boolean(env, "SAVE", True),
        fallback_refs=tuple(refs),
        force=_boolean(env, "FORCE", False),
        strict=_boolean(env, "STRICT", False),
        retries=retries,
        connect_timeout=_positive_timeout(env, "CONNECT_TIMEOUT", 10),
        read_timeout=_positive_timeout(env, "READ_TIMEOUT", 60),
    )


if __name__ == "__main__":
    # Setup and cleanup run on the host interpreter outside the per-job environment.
    try:
        validate_state_dir(_checkout(os.environ), Path(sys.argv[1]))
    except (ValueError, OSError, RuntimeError) as exc:
        sys.exit(f"qdb-cache: {exc}")
