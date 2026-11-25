"""Buildkite cache configuration and job state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Mapping
from urllib.parse import quote

CACHE_FORMAT_VERSION = "v1"
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
    parallelism: int

    @property
    def identity(self) -> str:
        return self.cache_prefix("")

    def cache_prefix(self, prefix: str, git_ref: str | None = None) -> str:
        # Hash portable specifications so checkout and home locations can differ across agents.
        paths = self.external_layout or tuple(p.as_posix() for p in self.root_paths)
        encoded_paths = json.dumps(sorted(paths), separators=(",", ":")).encode("ascii")
        parts = [
            prefix.strip("/"),
            self.pipeline,
            (git_ref or self.git_ref) if self.git_ref is not None else None,
            "cache",
            CACHE_FORMAT_VERSION,
            "paths-" + hashlib.sha256(encoded_paths).hexdigest(),
        ]
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(p for p in parts if p)


def _boolean(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _strings(value) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise ValueError("expected a string or a list of nonempty strings")
    return tuple(values)


def _pairs(items) -> dict[str, str]:
    pairs = {item["key"]: item["value"] for item in items}
    if "" in pairs or len(pairs) != len(items):
        raise ValueError("cache keys must be nonempty and unique")
    return dict(sorted(pairs.items()))


def _positive_integer(value, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_timeout(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _git_ref(env: Mapping[str, str], value) -> str | None:
    """Select the configured branch, shared namespace, or current Buildkite ref."""
    if isinstance(value, str):
        value = value.strip()
    if value is False or value == "false":
        return None
    if value is not True and value != "true":
        if not isinstance(value, str) or not value:
            raise ValueError("git-ref must be a boolean, branch name, or full Git ref")
        return value if value.startswith("refs/") else f"refs/heads/{value}"

    tag = env.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else f"refs/tags/{tag}"

    branch = env.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    raise ValueError("BUILDKITE_BRANCH or BUILDKITE_TAG is required")


def normalize_path(raw: str, *, allow_parent: bool = False) -> Path:
    """Only trusted pipeline roots may traverse a parent directory."""
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("path must be a nonempty checkout-relative name")
    path = Path(raw)
    if (
        path.anchor
        or (os.name != "nt" and "\\" in raw)
        or (".." in path.parts and not allow_parent)
        or (path.parts and path.parts[0].startswith("~"))
        or any(PureWindowsPath(part).drive for part in path.parts)
    ):
        raise ValueError(f"unsafe checkout-relative path: {raw!r}")
    return path


def _checkout(env: Mapping[str, str], cwd: Path | None = None) -> Path:
    return Path(env.get("BUILDKITE_BUILD_CHECKOUT_PATH") or cwd or Path.cwd()).resolve()


class CacheRootError(ValueError):
    """An invalid path configuration is fatal even for best-effort caches."""


def validate_state_dir(checkout: Path, state_dir: Path) -> None:
    """Keep every job's private state outside the checkout, including during setup and cleanup."""
    shared = state_dir.parent
    resolved = shared.resolve()
    if (
        shared.is_relative_to(checkout)
        or resolved.is_relative_to(checkout)
        or checkout.is_relative_to(resolved)
    ):
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
        if "\0" in raw or (os.name != "nt" and "\\" in raw):
            raise ValueError(f"invalid cache path: {raw!r}")
        if path.is_absolute() or (path.parts and path.parts[0] == "~"):
            external = True
            local = path.expanduser()
        else:
            local = checkout / normalize_path(raw, allow_parent=True)
            external |= ".." in path.parts
        if local.name == "..":
            local = local.resolve()
        if not local.name or path.as_posix() in locations:
            raise ValueError(f"filesystem root or duplicate cache path: {raw!r}")
        locations[path.as_posix()] = local

    specs = sorted(locations)
    if external:
        return {Path(str(i)): locations[s] for i, s in enumerate(specs)}, tuple(specs)
    return {Path(s): locations[s] for s in specs}, ()


def bind_local_roots(config: CacheConfig, *, clearing=False) -> CacheConfig:
    """Check live overlaps and bind external paths when an enabled operation starts."""
    checked = [config.state_dir.parent.parent.resolve(), config.state_dir.parent.resolve()]
    bound = {}
    for wire, configured in config.root_paths.items():
        # Cache the leaf itself (which may be a symlink), while comparing its real parent.
        local = configured.parent.resolve() / configured.name
        if any(local.is_relative_to(p) or p.is_relative_to(local) for p in checked):
            raise CacheRootError(
                f"cache root overlaps the plugin or another cache root: {str(local)!r}"
            )
        if clearing and (
            config.working_dir.is_relative_to(local) or Path.home().resolve().is_relative_to(local)
        ):
            raise CacheRootError(
                f"clearing this cache root would include the checkout or home: {str(local)!r}"
            )
        checked.append(local)
        # Explicit external paths opt into traversing agent-owned aliases.
        # Checkout-relative paths still traverse the checkout under the normal link checks.
        spec = Path(config.external_layout[int(wire.name)]) if config.external_layout else wire
        external = spec.is_absolute() or spec.parts[:1] == ("~",) or ".." in spec.parts
        bound[wire] = local if external else configured
    return replace(config, root_paths=bound)


def load_config(
    env: Mapping[str, str] | None = None, cwd: Path | None = None, plugin_dir: Path = PLUGIN_DIR
) -> CacheConfig:
    """Load cache paths, identity, fallback refs, and SDK options from Buildkite."""
    env = os.environ if env is None else env
    checkout = _checkout(env, cwd)
    options = json.loads(env["BUILDKITE_PLUGIN_CONFIGURATION"])
    if not isinstance(options, dict):
        raise ValueError("plugin configuration must be a JSON object")
    state_dir = plugin_dir / ".state" / env["BUILDKITE_JOB_ID"]
    validate_state_dir(checkout, state_dir)
    pipeline = env["BUILDKITE_PIPELINE_SLUG"]
    git_ref = _git_ref(env, options.get("git-ref", True))
    mode = options.get("fallback", "default")
    if mode not in {"default", "disabled"}:
        raise ValueError("fallback must be default or disabled")
    refs = [git_ref]
    if git_ref is not None and mode == "default":
        refs.extend(ref for ref in ("refs/heads/master", "refs/heads/main") if ref != git_ref)
    paths = _strings(options["path"])
    if not paths:
        raise ValueError("path: expected at least one value")
    roots, external_layout = _configured_roots(paths, checkout)
    return CacheConfig(
        root_paths=roots,
        external_layout=external_layout,
        working_dir=checkout,
        pairs=_pairs(options.get("keys", [])),
        exclusions=tuple(sorted(set(_strings(options.get("exclude", []))))),
        pipeline=pipeline,
        git_ref=git_ref,
        state_dir=state_dir,
        restore=_boolean(options.get("restore", True)),
        clear=_boolean(options.get("clear", True)),
        save=_boolean(options.get("save", True)),
        fallback_refs=tuple(refs),
        force=_boolean(options.get("force", False)),
        strict=_boolean(options.get("strict", False)),
        retries=_positive_integer(options.get("retries", 3), "retries"),
        parallelism=_positive_integer(
            options.get("parallelism", os.cpu_count() or 1), "parallelism"
        ),
        connect_timeout=_positive_timeout(options.get("connect-timeout", 10), "connect-timeout"),
        read_timeout=_positive_timeout(options.get("read-timeout", 60), "read-timeout"),
    )


if __name__ == "__main__":
    # Setup and cleanup run on the host interpreter outside the per-job environment.
    try:
        validate_state_dir(_checkout(os.environ), Path(sys.argv[1]))
    except (ValueError, OSError, RuntimeError) as exc:
        sys.exit(f"qdb-cache: {exc}")
