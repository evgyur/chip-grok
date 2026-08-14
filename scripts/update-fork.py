#!/usr/bin/env python3
"""Replay the Chip Grok patch stack on upstream and update the verified local fork.

The default mode is safe: fetch, prepare, and verify a candidate without publishing
or activating it. Public main rewrites require two explicit flags.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
from typing import Any

UPSTREAM_URL = "https://github.com/xai-org/grok-build.git"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class SyncError(RuntimeError):
    pass


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(f"command failed ({result.returncode}): {' '.join(args)}: {detail}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=repo, check=check).stdout.strip()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_remote(repo: Path, name: str, url: str) -> None:
    current = git(repo, "remote", "get-url", name, check=False)
    if current:
        if current != url:
            git(repo, "remote", "set-url", name, url)
    else:
        git(repo, "remote", "add", name, url)


def inspect_source(fork_repo: Path) -> dict[str, Any]:
    fork_url = git(fork_repo, "remote", "get-url", "origin")
    upstream_url = git(fork_repo, "remote", "get-url", "upstream")
    if not fork_url or not upstream_url:
        raise SyncError("fork repository must have origin and upstream remotes")
    git(fork_repo, "fetch", "--prune", "origin", "main")
    git(fork_repo, "fetch", "--prune", "upstream", "main")
    fork_head = git(fork_repo, "rev-parse", "origin/main")
    upstream_head = git(fork_repo, "rev-parse", "upstream/main")
    old_upstream = git(fork_repo, "merge-base", "origin/main", "upstream/main")
    for value in (fork_head, upstream_head, old_upstream):
        if not HEX40.fullmatch(value):
            raise SyncError("repository returned an invalid commit identity")
    patch_lines = git(
        fork_repo,
        "rev-list",
        "--reverse",
        "--parents",
        f"{old_upstream}..origin/main",
    ).splitlines()
    patch_commits: list[str] = []
    for line in patch_lines:
        fields = line.split()
        if len(fields) != 2:
            raise SyncError("fork patch stack must be linear and contain no merge commits")
        patch_commits.append(fields[0])
    if not patch_commits:
        raise SyncError("fork main contains no behavioral patch commits")
    return {
        "fork_url": fork_url,
        "upstream_url": upstream_url,
        "fork_head": fork_head,
        "upstream_head": upstream_head,
        "old_upstream": old_upstream,
        "patch_commits": patch_commits,
    }


def prepare_candidate(source: dict[str, Any], state_root: Path) -> dict[str, Any]:
    upstream_head = source["upstream_head"]
    candidate = state_root / "candidates" / upstream_head[:12]
    if candidate.exists():
        raise SyncError(f"candidate already exists and was preserved: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--quiet", "--no-checkout", source["fork_url"], str(candidate)])
    ensure_remote(candidate, "upstream", source["upstream_url"])
    git(candidate, "fetch", "--prune", "origin", "main")
    git(candidate, "fetch", "--prune", "upstream", "main")
    branch = f"sync/upstream-{upstream_head[:12]}"
    git(candidate, "checkout", "-B", branch, "origin/main")
    rebase = run(
        [
            "git",
            "rebase",
            "--onto",
            "upstream/main",
            source["old_upstream"],
            branch,
        ],
        cwd=candidate,
        check=False,
    )
    if rebase.returncode != 0:
        conflicts = git(candidate, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
        report = {
            **source,
            "status": "conflict",
            "candidate": str(candidate),
            "conflicts": conflicts,
            "error": (rebase.stderr or rebase.stdout).strip()[-8000:],
        }
        atomic_json(candidate / ".chip-sync-report.json", report)
        return report
    candidate_head = git(candidate, "rev-parse", "HEAD")
    candidate_base = git(candidate, "merge-base", "HEAD", "upstream/main")
    candidate_patches = git(candidate, "rev-list", "--reverse", f"upstream/main..HEAD").splitlines()
    if candidate_base != upstream_head:
        raise SyncError("candidate is not based on the fetched upstream head")
    if len(candidate_patches) != len(source["patch_commits"]):
        raise SyncError("candidate patch count changed during replay")
    report = {
        **source,
        "status": "candidate_ready",
        "candidate": str(candidate),
        "candidate_head": candidate_head,
        "candidate_patch_commits": candidate_patches,
    }
    atomic_json(candidate / ".chip-sync-report.json", report)
    return report


def verify_and_build(report: dict[str, Any], state_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    candidate = Path(report["candidate"])
    upstream_head = report["upstream_head"]
    fork_head = report["candidate_head"]
    log_path = candidate / ".chip-sync-verify.log"
    artifact_dir = state_root / "artifacts" / fork_head
    artifact_dir.mkdir(parents=True, exist_ok=True)
    binary = artifact_dir / "grok"
    image = "rust@sha256:365468470075493dc4583f47387001854321c5a8583ea9604b297e67f01c5a4f"
    run(["docker", "volume", "create", "chip-grok-tools"])
    run(
        [
            "docker", "run", "--rm",
            "-e", "PATH=/usr/local/cargo/bin:/usr/local/rustup/toolchains/1.94.0-x86_64-unknown-linux-gnu/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "-v", "chip-grok-cargo-registry:/usr/local/cargo/registry",
            "-v", "chip-grok-cargo-git:/usr/local/cargo/git",
            "-v", "chip-grok-tools:/tools",
            image,
            "bash", "-c",
            "set -e; if [ ! -x /tools/bin/dotslash ]; then cargo install --locked --root /tools dotslash; fi",
        ]
    )
    target_volume = f"chip-grok-target-{upstream_head[:12]}"
    verify_script = " ".join(
        [
            "set -euo pipefail;",
            "cargo fmt --all -- --check;",
            "cargo check --locked -p xai-grok-pager-bin;",
            "cargo test --locked -p xai-grok-pager-bin --bin xai-grok-pager worker_contract::tests --no-fail-fast;",
            "cargo build --locked --release -p xai-grok-pager-bin --bin xai-grok-pager;",
            "PAGER_BINARY=/target/release/xai-grok-pager cargo test --locked -p xai-grok-pager-bin --test worker_contract_e2e -- --ignored;",
            "PAGER_BINARY=/target/release/xai-grok-pager cargo test --locked -p xai-grok-pager-bin --test update_never_blocked_by_config;",
            "install -m 700 /target/release/xai-grok-pager /out/grok;",
            "chown $HOST_UID:$HOST_GID /out/grok;",
        ]
    )
    command = [
        "docker",
        "run",
        "--rm",
        "-e",
        f"CHIP_UPSTREAM_REVISION={upstream_head}",
        "-e",
        f"CHIP_FORK_REVISION={fork_head}",
        "-e",
        "CARGO_BUILD_JOBS=4",
        "-e",
        "CARGO_TARGET_DIR=/target",
        "-e",
        "PATH=/tools/bin:/usr/local/cargo/bin:/usr/local/rustup/toolchains/1.94.0-x86_64-unknown-linux-gnu/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "-e",
        f"HOST_UID={os.getuid()}",
        "-e",
        f"HOST_GID={os.getgid()}",
        "-v",
        f"{candidate}:/src:ro",
        "-v",
        "chip-grok-cargo-registry:/usr/local/cargo/registry",
        "-v",
        "chip-grok-cargo-git:/usr/local/cargo/git",
        "-v",
        f"{target_volume}:/target",
        "-v",
        "chip-grok-tools:/tools",
        "-v",
        f"{artifact_dir}:/out",
        "-w",
        "/src",
        image,
        "bash",
        "-c",
        verify_script,
    ]
    with log_path.open("w") as log:
        log.write(f"$ docker run {image} <bounded local gate>\n")
        log.flush()
        result = run(command, check=False, stdout=log, stderr=log)
    if result.returncode != 0:
        raise SyncError(f"container verification failed; log={log_path}")
    if not binary.is_file():
        raise SyncError("release build did not export xai-grok-pager")
    env = {
        **os.environ,
        "HOME": str(real_home()),
        "CHIP_UPSTREAM_REVISION": upstream_head,
        "CHIP_FORK_REVISION": fork_head,
    }
    version = json.loads(run([str(binary), "version", "--json"], env=env).stdout)
    expected = {
        "distribution": "chip",
        "fork_commit": fork_head,
        "upstream_commit": upstream_head,
        "worker_contracts": [1],
        "auto_update": "externally-managed",
    }
    for key, value in expected.items():
        if version.get(key) != value:
            raise SyncError(f"built binary version evidence mismatch for {key}")
    raw_version = str(version.get("version", "0.0.0"))
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "-", raw_version)
    tag = f"chip-v{safe_version}.{upstream_head[:7]}.{fork_head[:7]}"
    lock = {
        "schema": 1,
        "repository": report["fork_url"],
        "tag": tag,
        "version": raw_version,
        "binary_path": "grok",
        "fork_commit": fork_head,
        "upstream_commit": upstream_head,
        "upstream_source_rev": version["upstream_source_rev"],
        "platform": "linux-x86_64",
        "sha256": sha256(binary),
    }
    lock_path = candidate / ".chip-fork.lock.json"
    atomic_json(lock_path, lock)
    report = {**report, "status": "verified", "binary": str(binary), "lock": lock}
    atomic_json(candidate / ".chip-sync-report.json", report)
    return binary, lock_path, report


def h20_smoke(binary: Path, lock_path: Path, adapter_root: Path, state_root: Path) -> dict[str, Any]:
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke-", dir=state_root))
    repo = smoke_root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "chip-grok smoke")
    git(repo, "config", "user.email", "smoke@localhost")
    (repo / "README.md").write_text("# chip-grok smoke\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "smoke baseline")
    env = {
        **os.environ,
        "HOME": str(real_home()),
        "CHIP_GROK_BIN": str(binary),
        "CHIP_GROK_LOCK_FILE": str(lock_path),
        "CHIP_GROK_MODEL": "h20-gpt",
        "CHIP_GROK_PASSTHROUGH_ENV": "H20_FUSION_API_KEY",
        "CHIP_GROK_TIMEOUT": "600",
        "CHIP_GROK_WORKTREE_ROOT": str(smoke_root / "worktrees"),
    }
    command = [
        "python3",
        str(adapter_root / "scripts" / "chip_grok.py"),
        "run",
        "--repo",
        str(repo),
        "--task",
        "Inspect README.md. Do not modify files. Complete successfully after confirming the repository is readable.",
        "--trusted-worker",
    ]
    result = run(command, env=env, check=False)
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SyncError("H20 smoke did not return a JSON receipt") from error
    if result.returncode != 0 or receipt.get("status") != "completed":
        raise SyncError(f"H20 smoke failed: {receipt.get('error') or result.stderr}")
    return {"status": receipt["status"], "model": receipt.get("model_alias")}


def publish_and_activate(
    report: dict[str, Any],
    binary: Path,
    lock_path: Path,
    adapter_root: Path,
    install_root: Path,
) -> dict[str, Any]:
    candidate = Path(report["candidate"])
    old_head = report["fork_head"]
    new_head = report["candidate_head"]
    archive_tag = f"archive/pre-upstream-{report['upstream_head'][:12]}-{old_head[:12]}"
    git(candidate, "push", "origin", f"{old_head}:refs/tags/{archive_tag}")
    git(
        candidate,
        "push",
        f"--force-with-lease=refs/heads/main:{old_head}",
        "origin",
        f"{new_head}:refs/heads/main",
    )
    tag = report["lock"]["tag"]
    git(candidate, "tag", "-f", tag, new_head)
    git(candidate, "push", "origin", f"refs/tags/{tag}")
    run(
        [
            "bash",
            str(adapter_root / "scripts" / "install-fork.sh"),
            "install",
            "--root",
            str(install_root),
            "--binary",
            str(binary),
            "--lock",
            str(lock_path),
        ]
    )
    return {**report, "status": "activated", "archive_tag": archive_tag}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fork-repo", type=Path, required=True)
    parser.add_argument("--adapter-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-root", type=Path, default=real_home() / ".local" / "state" / "chip-grok-sync")
    parser.add_argument("--install-root", type=Path, default=real_home() / ".local" / "lib" / "chip-grok")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--h20-smoke", action="store_true")
    parser.add_argument("--publish-and-activate", action="store_true")
    parser.add_argument("--allow-main-rewrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.publish_and_activate and not args.allow_main_rewrite:
        raise SyncError("publishing requires --allow-main-rewrite")
    if args.h20_smoke and not args.verify:
        raise SyncError("H20 smoke requires --verify")
    if args.publish_and_activate and not args.h20_smoke:
        raise SyncError("publishing requires a successful H20 smoke")
    args.state_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (args.state_root / "sync.lock").open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise SyncError("another upstream sync is already running") from exc
    source = inspect_source(args.fork_repo.resolve())
    if source["old_upstream"] == source["upstream_head"]:
        return 0
    report = prepare_candidate(source, args.state_root.resolve())
    if report["status"] == "conflict":
        print(
            "BLOCKED upstream replay conflict: "
            + ", ".join(report["conflicts"])
            + f"; candidate={report['candidate']}"
        )
        return 20
    binary: Path | None = None
    lock_path: Path | None = None
    if args.verify:
        binary, lock_path, report = verify_and_build(report, args.state_root.resolve())
    if args.h20_smoke:
        if binary is None or lock_path is None:
            raise SyncError("verified binary and lock are unavailable")
        report["h20_smoke"] = h20_smoke(
            binary, lock_path, args.adapter_root.resolve(), args.state_root.resolve()
        )
        report["status"] = "smoke_passed"
        atomic_json(Path(report["candidate"]) / ".chip-sync-report.json", report)
    if args.publish_and_activate:
        if binary is None or lock_path is None:
            raise SyncError("verified binary and lock are unavailable")
        report = publish_and_activate(
            report,
            binary,
            lock_path,
            args.adapter_root.resolve(),
            args.install_root.resolve(),
        )
        atomic_json(Path(report["candidate"]) / ".chip-sync-report.json", report)
    print(
        f"{report['status'].upper()} upstream={report['upstream_head']} "
        f"fork={report.get('candidate_head', report['fork_head'])} candidate={report['candidate']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"BLOCKED {error}", file=sys.stderr)
        raise SystemExit(20)
