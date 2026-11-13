"""Buildkite cache configuration and job state."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

PREFIX = "BUILDKITE_PLUGIN_QDB_CACHE_"
FORMAT = "1"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CacheRoot:
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
        payload = {"roots": [r.specification for r in self.roots], "exclude": self.exclusions}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def identity(self) -> str:
        return "/".join(
            [self.pipeline, self.git_ref]
            + [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items()]
        )

    def object_key(self, prefix: str, git_ref: str | None = None) -> str:
        parts = [prefix.strip("/"), self.pipeline, git_ref or self.git_ref, "cache", "v1"]
        parts.extend(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in self.pairs.items())
        return "/".join(p for p in parts if p) + "/snapshot.tar.zst"


def job_state_dir(job_id: str, plugin_dir: Path = PLUGIN_DIR) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id):
        raise ValueError(
            "BUILDKITE_JOB_ID must contain only letters, digits, underscores or hyphens"
        )
    return plugin_dir / ".state" / job_id


def validate_state_dir(state: Path) -> None:
    # Windows junctions must be rejected even on Python without Path.is_junction.
    for path in (state.parent, state, state / ".venv"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_reparse_tag", 0) == getattr(
            stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003
        ):
            raise ValueError(f"refusing redirected or non-directory job state: {str(path)!r}")


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(PREFIX + name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"true", "on", "yes", "1"}:
        return True
    if normalized in {"false", "off", "no", "0"}:
        return False
    raise ValueError(f"{name.lower()}: invalid boolean {value!r}")


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    value = env.get(PREFIX + name, str(default))
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{name.lower()}: expected positive seconds") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name.lower()}: expected positive seconds")
    return number


def _strings(env: Mapping[str, str], name: str, required: bool = False) -> tuple[str, ...]:
    prefix = PREFIX + name
    indexed = {k[len(prefix) + 1 :]: v for k, v in env.items() if k.startswith(prefix + "_")}
    if indexed:
        if prefix in env or any(not re.fullmatch(r"0|[1-9][0-9]*", i) for i in indexed):
            raise ValueError(f"{name.lower()}: malformed indexed list")
        indices = sorted(int(i) for i in indexed)
        if indices != list(range(len(indices))):
            raise ValueError(f"{name.lower()}: list indices must be contiguous from zero")
        values = tuple(indexed[str(i)] for i in indices)
    else:
        values = (env[prefix],) if prefix in env else ()
    if (required and not values) or any(not v.strip() or "\0" in v for v in values):
        raise ValueError(f"{name.lower()}: expected nonempty strings")
    return values


def _pairs(env: Mapping[str, str]) -> dict[str, str]:
    prefix = PREFIX + "KEYS"
    if prefix in env:
        raise ValueError("keys: expected indexed key/value records")
    records: dict[int, dict[str, str]] = {}
    for name, value in env.items():
        if not name.startswith(prefix + "_"):
            continue
        match = re.fullmatch(re.escape(prefix) + r"_(0|[1-9][0-9]*)_(KEY|VALUE)", name)
        if not match:
            raise ValueError("keys: malformed key/value record")
        records.setdefault(int(match[1]), {})[match[2]] = value
    if sorted(records) != list(range(len(records))):
        raise ValueError("keys: record indices must be contiguous from zero")
    pairs = {}
    for record in records.values():
        if set(record) != {"KEY", "VALUE"} or not record["KEY"].strip():
            raise ValueError("keys: each record requires a nonempty key and a string value")
        if "\0" in record["KEY"] or "\0" in record["VALUE"]:
            raise ValueError("keys: NUL characters are invalid")
        if record["KEY"] in pairs:
            raise ValueError("keys: duplicate keys")
        pairs[record["KEY"]] = record["VALUE"]
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


def _validate_ref(ref: str) -> str:
    if any(p in {"", ".", ".."} for p in ref.split("/")) or any(c in ref for c in "\\\0\r\n"):
        raise ValueError(f"invalid git ref: {ref!r}")
    return ref


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _normalized_path(path: str) -> str:
    # A home anchor must survive normalization: ~/../cache is relative to the
    # expanded home parent; discarding the anchor would select the checkout.
    portable = path.replace(os.sep, "/")
    if portable.startswith("~") and "/" in portable:
        home, tail = portable.split("/", 1)
        tail = posixpath.normpath(tail)
        return home if tail == "." else f"{home}/{tail}"
    return os.path.normpath(path).replace(os.sep, "/")


def _roots(paths: tuple[str, ...], cwd: Path, state: Path) -> tuple[CacheRoot, ...]:
    specifications = sorted(_normalized_path(p) for p in paths)
    roots = []
    for index, spec in enumerate(specifications):
        expanded = Path(spec).expanduser()
        if spec.startswith("~") and str(expanded).startswith("~"):
            raise ValueError(f"cannot expand home path: {spec!r}")
        destination = Path(os.path.abspath(cwd / expanded))
        resolved = destination.resolve()
        if destination.parent == destination or resolved.parent == resolved:
            raise ValueError(f"filesystem roots cannot be cached: {spec!r}")
        if _overlap(resolved, state.parent.resolve()) or _overlap(destination, state.parent):
            raise ValueError(f"cache root overlaps plugin state: {spec!r}")
        if any(
            _overlap(destination, r.destination) or _overlap(resolved, r.destination.resolve())
            for r in roots
        ):
            raise ValueError(f"duplicate or overlapping cache root: {spec!r}")
        roots.append(CacheRoot(spec, destination, f"roots/{index:04d}"))
    return tuple(roots)


def load_config(
    env: Mapping[str, str] | None = None, cwd: Path | None = None, plugin_dir: Path = PLUGIN_DIR
) -> CacheConfig:
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    state = job_state_dir(env.get("BUILDKITE_JOB_ID", ""), plugin_dir)
    pipeline = env.get("BUILDKITE_PIPELINE_SLUG", "")
    if not pipeline.strip() or pipeline in {".", ".."} or any(c in pipeline for c in "/\\\0\r\n"):
        raise ValueError("BUILDKITE_PIPELINE_SLUG is missing or invalid")
    git_ref = _current_buildkite_git_ref(env)
    if git_ref is None:
        raise ValueError("BUILDKITE_BRANCH or BUILDKITE_TAG is required")
    _validate_ref(git_ref)
    mode = env.get(PREFIX + "FALLBACK", "default")
    if mode not in {"default", "custom", "disabled"}:
        raise ValueError("fallback must be default, custom or disabled")
    branches = ("master", "main") if mode == "default" else ()
    if mode == "custom":
        branches = _strings(env, "FALLBACK_BRANCHES", required=True)
    refs = [git_ref]
    for branch in branches:
        if branch.startswith("refs/") and not branch.startswith("refs/heads/"):
            raise ValueError("fallback-branches accepts branch names or refs/heads/... refs")
        ref = _validate_ref(branch if branch.startswith("refs/") else f"refs/heads/{branch}")
        if ref not in refs:
            refs.append(ref)
    retries = env.get(PREFIX + "RETRIES", "3")
    if not re.fullmatch(r"[1-9][0-9]*", retries):
        raise ValueError("retries must be a positive integer")
    try:
        roots = _roots(_strings(env, "PATH", required=True), cwd, state)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid cache paths: {exc}") from exc
    return CacheConfig(
        roots=roots,
        pairs=_pairs(env),
        exclusions=tuple(sorted(set(_strings(env, "EXCLUDE")))),
        pipeline=pipeline,
        git_ref=git_ref,
        job_id=env["BUILDKITE_JOB_ID"],
        state_dir=state,
        restore=_boolean(env, "RESTORE", True),
        save=_boolean(env, "SAVE", True),
        fallback_refs=tuple(refs),
        force=_boolean(env, "FORCE", False),
        strict=_boolean(env, "STRICT", False),
        retries=int(retries),
        connect_timeout=_number(env, "CONNECT_TIMEOUT", 10),
        read_timeout=_number(env, "READ_TIMEOUT", 60),
        debug=_boolean(env, "DEBUG", False),
    )


# Setup and teardown run before or after the venv exists, so this entry point
# depends only on the standard library.
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate or remove the current job's state")
    parser.add_argument("command", choices=("validate-state", "cleanup"))
    args = parser.parse_args()
    try:
        state = job_state_dir(os.environ.get("BUILDKITE_JOB_ID", ""))
        validate_state_dir(state)
        if args.command == "cleanup" and state.exists():
            shutil.rmtree(state)
    except (ValueError, OSError) as exc:
        print(f"[qdb-cache] {exc}", file=sys.stderr)
        sys.exit(0 if args.command == "cleanup" else 1)
