#!/usr/bin/env python3
"""Minimal validation helpers for the pinned Grok Build fork."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
from typing import Any

LOCK_KEYS = {
    "schema", "repository", "tag", "version", "binary_path", "fork_commit",
    "upstream_commit", "upstream_source_rev", "platform", "sha256",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VERSION_TIMEOUT = 15

class ContractError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return payload


def sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return sha256_fd(fd)
    finally:
        os.close(fd)


def validate_lock(payload: dict[str, Any], *, allow_placeholder: bool = False) -> dict[str, Any]:
    if set(payload) != LOCK_KEYS:
        raise ContractError("fork lock must contain exactly the strict schema fields")
    if payload["schema"] != 1:
        raise ContractError("unsupported fork lock schema")
    if payload["repository"] != "https://github.com/evgyur/grok-build":
        raise ContractError("unexpected fork repository")
    if payload["binary_path"] != "grok":
        raise ContractError("fork binary_path must be the release-local grok file")
    nullable = ("tag", "version", "fork_commit", "sha256")
    null_count = sum(payload[name] is None for name in nullable)
    if allow_placeholder and null_count == len(nullable):
        pass
    else:
        if null_count:
            raise ContractError("fork lock placeholder fields must be all null or all populated")
        if not isinstance(payload["tag"], str) or not re.fullmatch(r"chip-v[0-9A-Za-z._-]+", payload["tag"]):
            raise ContractError("invalid fork release tag")
        if not isinstance(payload["version"], str) or not re.fullmatch(r"[0-9A-Za-z._+-]+", payload["version"]):
            raise ContractError("invalid fork version")
        if not isinstance(payload["fork_commit"], str) or not HEX40.fullmatch(payload["fork_commit"]):
            raise ContractError("invalid fork commit")
        if not isinstance(payload["sha256"], str) or not HEX64.fullmatch(payload["sha256"]):
            raise ContractError("invalid fork binary sha256")
    for name in ("upstream_commit", "upstream_source_rev"):
        if not isinstance(payload[name], str) or not HEX40.fullmatch(payload[name]):
            raise ContractError(f"invalid {name}")
    if payload["platform"] != "linux-x86_64":
        raise ContractError("unsupported fork platform")
    return payload

def terminate_process_group(process: subprocess.Popen[str], grace: float = 0.5) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM if grace else signal.SIGKILL)
    except ProcessLookupError:
        return
    if not grace:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def version_provenance(binary: Path, *, binary_fd: int | None = None) -> dict[str, Any]:
    command = f"/proc/self/fd/{binary_fd}" if binary_fd is not None else str(binary)
    process = subprocess.Popen(
        [command, "version", "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=() if binary_fd is None else (binary_fd,),
    )
    try:
        stdout, _ = process.communicate(timeout=VERSION_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_process_group(process, grace=0)
            process.communicate()
        raise ContractError("fork binary version preflight timed out") from exc
    finally:
        terminate_process_group(process)
    if process.returncode != 0:
        raise ContractError("fork binary version preflight failed")
    try:
        provenance = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("fork binary returned invalid version JSON") from exc
    if not isinstance(provenance, dict):
        raise ContractError("fork binary version provenance must be an object")
    return provenance


def verify_fork_fd(binary_fd: int, binary: Path, lock_path: Path) -> dict[str, Any]:
    lock = validate_lock(read_json(lock_path))
    info = os.fstat(binary_fd)
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise ContractError("fork binary must be an executable regular file, not a symlink")
    actual_hash = sha256_fd(binary_fd)
    if actual_hash != lock["sha256"]:
        raise ContractError("fork binary sha256 does not match fork lock")
    provenance = version_provenance(binary, binary_fd=binary_fd)
    expected = {
        "distribution": "chip",
        "version": lock["version"],
        "fork_commit": lock["fork_commit"],
        "upstream_commit": lock["upstream_commit"],
        "upstream_source_rev": lock["upstream_source_rev"],
        "auto_update": "externally-managed",
    }
    for name, value in expected.items():
        if provenance.get(name) != value:
            raise ContractError(f"fork provenance {name} mismatch")
    if 1 not in provenance.get("worker_contracts", []):
        raise ContractError("fork does not advertise worker contract v1")
    return {"status": "verified", "binary": str(binary.resolve()), "sha256": actual_hash, "tag": lock["tag"], "provenance": provenance}


def verify_fork(binary: Path, lock_path: Path) -> dict[str, Any]:
    try:
        binary_fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ContractError("fork binary must be an executable regular file, not a symlink") from exc
    try:
        return verify_fork_fd(binary_fd, binary, lock_path)
    finally:
        os.close(binary_fd)
