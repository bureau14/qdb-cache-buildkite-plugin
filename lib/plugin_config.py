"""Buildkite cache configuration and job state."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

PREFIX = "BUILDKITE_PLUGIN_QDB_CACHE_"
CACHE_FORMAT_VERSION = "1"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CacheConfig:
    roots: tuple[Path, ...]
    working_dir: Path
    pairs: dict[str, str]
    exclusions: tuple[str, ...]
    pipeline: str
    git_ref: str
    state_dir: Path
    restore: bool
    save: bool
    fallback_refs: tuple[str, ...]
    force: bool
    strict: bool
    retries: int
    connect_timeout: float
    read_timeout: float

    @property
    def layout_digest(self) -> str:
        # Relative archive paths and exclusions distinguish compatible snapshots.
        # The paths field keeps indexed-root snapshots from matching this layout.
        payload = {"paths": [root.as_posix() for root in self.roots], "exclude": self.exclusions}
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
        parts = [
            prefix.strip("/"),
            self.pipeline,
            git_ref or self.git_ref,
            "cache",
            f"v{CACHE_FORMAT_VERSION}",
        ]
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(p for p in parts if p) + "/snapshot.tar.zst"


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


def _roots(paths: tuple[str, ...]) -> tuple[Path, ...]:
    roots = tuple(sorted(Path(os.path.normpath(path)) for path in paths))
    if any(root.anchor or ".." in root.parts for root in roots):
        raise ValueError("path must be relative to the working directory")
    return roots


def load_config(
    env: Mapping[str, str] | None = None, cwd: Path | None = None, plugin_dir: Path = PLUGIN_DIR
) -> CacheConfig:
    """Load cache paths, identity, fallback refs, and SDK options from Buildkite."""
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
        roots=_roots(_strings(env, "PATH", required=True)),
        working_dir=cwd,
        pairs=_pairs(env),
        exclusions=tuple(sorted(set(_strings(env, "EXCLUDE")))),
        pipeline=pipeline,
        git_ref=git_ref,
        state_dir=plugin_dir / ".state" / job_id,
        restore=_boolean(env, "RESTORE", True),
        save=_boolean(env, "SAVE", True),
        fallback_refs=tuple(refs),
        force=_boolean(env, "FORCE", False),
        strict=_boolean(env, "STRICT", False),
        retries=retries,
        connect_timeout=_positive_timeout(env, "CONNECT_TIMEOUT", 10),
        read_timeout=_positive_timeout(env, "READ_TIMEOUT", 60),
    )
