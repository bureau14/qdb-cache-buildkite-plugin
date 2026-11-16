"""Buildkite cache configuration and job state."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

PREFIX = "BUILDKITE_PLUGIN_QDB_CACHE_"
CACHE_FORMAT_VERSION = "1"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CacheRoot:
    """Keep portable layout identity separate from agent-specific destinations and tar names."""

    specification: str
    destination: Path
    archive_name: str


@dataclass(frozen=True)
class CacheConfig:
    roots: tuple[CacheRoot, ...]
    pairs: dict[str, str]
    exclusions: tuple[str, ...]
    pipeline: str
    git_ref: str
    job_id: str
    state_dir: Path
    restore: bool
    save: bool
    fallback_refs: tuple[str, ...]
    force: bool
    strict: bool
    retries: int
    connect_timeout: float
    read_timeout: float
    debug: bool

    @property
    def layout_digest(self) -> str:
        # Logical home paths stay independent of the username on the restoring agent.
        # Include exclusions so snapshots with different filtered content are incompatible.
        payload = {"roots": [r.specification for r in self.roots], "exclude": self.exclusions}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def identity(self) -> str:
        # Encode delimiters so diagnostics distinguish keys containing "/" or "=".
        return "/".join(
            [self.pipeline, self.git_ref]
            + [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items()]
        )

    def object_key(self, prefix: str, git_ref: str | None = None) -> str:
        parts = [prefix.strip("/"), self.pipeline, git_ref or self.git_ref, "cache", "v1"]
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(p for p in parts if p) + "/snapshot.tar.zst"


def job_state_dir(job_id: str, plugin_dir: Path = PLUGIN_DIR) -> Path:
    return plugin_dir / ".state" / job_id


# Boolean/list parsing follows qdb-artifacts' _get_env_bool/_get_env_array helpers.
def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(PREFIX + name)
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _strings(env: Mapping[str, str], name: str, required: bool = False) -> tuple[str, ...]:
    prefix = PREFIX + name
    values = []
    while value := env.get(f"{prefix}_{len(values)}"):
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
        if key in pairs:
            raise ValueError("keys: duplicate keys")
        pairs[key] = env[f"{PREFIX}KEYS_{index}_VALUE"]
        index += 1
    return dict(sorted(pairs.items()))


def _current_buildkite_git_ref(env: Mapping[str, str]) -> str | None:
    """Return the current Buildkite git ref in artifact namespace format, if known."""
    tag = env.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else f"refs/tags/{tag}"

    branch = env.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    return None


def _normalized_path(path: str) -> str:
    # A home anchor must survive normalization: ~/../cache is relative to the
    # expanded home parent; discarding the anchor would select the checkout.
    portable = path.replace(os.sep, "/")
    if portable.startswith("~") and "/" in portable:
        home, tail = portable.split("/", 1)
        tail = posixpath.normpath(tail)
        return home if tail == "." else f"{home}/{tail}"
    return os.path.normpath(path).replace(os.sep, "/")


def _roots(paths: tuple[str, ...], cwd: Path) -> tuple[CacheRoot, ...]:
    specifications = sorted(_normalized_path(p) for p in paths)
    return tuple(
        CacheRoot(spec, Path(os.path.abspath(cwd / Path(spec).expanduser())), f"roots/{index:04d}")
        for index, spec in enumerate(specifications)
    )


def load_config(
    env: Mapping[str, str] | None = None, cwd: Path | None = None, plugin_dir: Path = PLUGIN_DIR
) -> CacheConfig:
    # This function is responsible for loading hook configuration. It works by:
    # 1. Reading identity from the Buildkite environment.
    # 2. Normalizing fallback refs while preserving the configured order.
    # 3. Mapping logical paths and dimensions to stable archive and storage names.
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    job_id = env["BUILDKITE_JOB_ID"]
    pipeline = env["BUILDKITE_PIPELINE_SLUG"]
    git_ref = _current_buildkite_git_ref(env)
    if git_ref is None:
        raise ValueError("BUILDKITE_BRANCH or BUILDKITE_TAG is required")
    mode = env.get(PREFIX + "FALLBACK", "default")
    if mode not in {"default", "disabled"}:
        raise ValueError("fallback must be default or disabled")
    refs = [git_ref]
    if mode == "default":
        refs.extend(ref for ref in ("refs/heads/master", "refs/heads/main") if ref != git_ref)
    try:
        retries = int(env.get(PREFIX + "RETRIES", "3"))
    except ValueError as exc:
        raise ValueError("retries must be a positive integer") from exc
    if retries <= 0:
        raise ValueError("retries must be a positive integer")
    return CacheConfig(
        roots=_roots(_strings(env, "PATH", required=True), cwd),
        pairs=_pairs(env),
        exclusions=tuple(sorted(set(_strings(env, "EXCLUDE")))),
        pipeline=pipeline,
        git_ref=git_ref,
        job_id=job_id,
        state_dir=job_state_dir(job_id, plugin_dir),
        restore=_boolean(env, "RESTORE", True),
        save=_boolean(env, "SAVE", True),
        fallback_refs=tuple(refs),
        force=_boolean(env, "FORCE", False),
        strict=_boolean(env, "STRICT", False),
        retries=retries,
        connect_timeout=float(env.get(PREFIX + "CONNECT_TIMEOUT", "10")),
        read_timeout=float(env.get(PREFIX + "READ_TIMEOUT", "60")),
        debug=_boolean(env, "DEBUG", False),
    )
