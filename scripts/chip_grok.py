#!/usr/bin/env python3
"""Run Grok Build as a reviewed coding worker in a dedicated git worktree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

DEFAULT_MAX_TURNS = 60
DEFAULT_TIMEOUT = 1800
MAX_RECEIPT_TEXT = 200_000
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN_NAME = re.compile(r"^chip-grok-[0-9]+-[a-f0-9]{8}$")


class ChipGrokError(RuntimeError):
    pass


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill the whole worker process group, not only the direct Grok process.
        # Otherwise shell/tool descendants can outlive the bounded run.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from exc
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ChipGrokError(f"command failed ({result.returncode}): {detail}")
    return result


def resolve_repo(raw: str) -> Path:
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ChipGrokError("repository path does not exist or is not a directory")
    root = run_command(["git", "rev-parse", "--show-toplevel"], cwd=repo).stdout.strip()
    return Path(root).resolve()


def git_text(repo: Path, *args: str) -> str:
    return run_command(["git", *args], cwd=repo).stdout.strip()


def git_status_raw(repo: Path) -> str:
    return run_command(["git", "status", "--porcelain=v1", "-z"], cwd=repo).stdout


def repo_fingerprint(repo: Path) -> dict[str, str]:
    """Capture source HEAD, index tree, and worktree status."""
    head = git_text(repo, "rev-parse", "HEAD")
    index = run_command(["git", "write-tree"], cwd=repo, check=False)
    index_tree = index.stdout.strip() if index.returncode == 0 else f"ERROR:{index.stderr.strip()}"
    return {"head": head, "index_tree": index_tree, "status_raw": git_status_raw(repo)}


def status_paths(raw: str) -> list[str]:
    """Parse porcelain-v1 -z output, including rename/copy two-path records."""
    entries = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        item = entries[index]
        if not item:
            index += 1
            continue
        status = item[:2]
        path = item[3:] if len(item) >= 4 else item
        paths.append(path)
        if "R" in status or "C" in status:
            # With -z, the current path is in the status record and the
            # original path follows as the next NUL-delimited field.
            index += 1
        index += 1
    return sorted(set(paths))


def worktree_root() -> Path:
    configured = os.getenv("CHIP_GROK_WORKTREE_ROOT", "").strip()
    root = Path(configured).expanduser().resolve() if configured else Path(tempfile.gettempdir()) / "chip-grok-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def ownership_root() -> Path:
    root = worktree_root() / ".ownership"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def manifest_path(token: str) -> Path:
    if not TOKEN_NAME.fullmatch(token):
        raise ChipGrokError("invalid chip-grok run token")
    return ownership_root() / f"{token}.json"


def write_manifest(payload: dict[str, object]) -> None:
    path = manifest_path(str(payload["run_token"]))
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    path.chmod(0o600)


def load_manifest(repo: Path, worktree: Path, token: str) -> dict[str, object]:
    path = manifest_path(token)
    if not path.is_file():
        raise ChipGrokError("ownership receipt not found; refusing cleanup")
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ChipGrokError("ownership receipt is invalid; refusing cleanup") from exc
    expected = {
        "format": 1,
        "run_token": token,
        "repo": str(repo.resolve()),
        "worktree": str(worktree.resolve()),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ChipGrokError("ownership receipt mismatch; refusing cleanup")
    return payload


def prepare_worktree(repo: Path) -> dict[str, object]:
    head = git_text(repo, "rev-parse", "HEAD")
    branch = git_text(repo, "branch", "--show-current") or "DETACHED"
    source = repo_fingerprint(repo)
    token = f"chip-grok-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    destination = worktree_root() / token
    run_command(["git", "worktree", "add", "--detach", str(destination), head], cwd=repo)
    manifest = {
        "format": 1,
        "run_token": token,
        "repo": str(repo),
        "worktree": str(destination),
        "base_head": head,
        "created_at": int(time.time()),
    }
    write_manifest(manifest)
    return {
        **manifest,
        "base_branch": branch,
        "source_dirty": bool(source["status_raw"]),
        "source_status": status_paths(source["status_raw"]),
        "source_fingerprint": source,
    }


def grok_command() -> list[str]:
    configured = os.getenv("CHIP_GROK_BIN", "grok").strip()
    parts = shlex.split(configured)
    if not parts:
        raise ChipGrokError("CHIP_GROK_BIN is empty")
    executable = shutil.which(parts[0]) if not Path(parts[0]).is_absolute() else parts[0]
    if not executable or not Path(executable).exists():
        raise ChipGrokError("Grok executable or wrapper was not found")
    parts[0] = str(executable)
    return parts


def worker_environment() -> tuple[dict[str, str], list[str]]:
    """Build a minimal child environment plus explicitly scoped provider vars."""
    baseline = {
        "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
        "NO_COLOR", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    requested = {
        item.strip()
        for item in os.getenv("CHIP_GROK_PASSTHROUGH_ENV", "").split(",")
        if item.strip()
    }
    invalid = sorted(name for name in requested if not ENV_NAME.fullmatch(name))
    if invalid:
        raise ChipGrokError("invalid CHIP_GROK_PASSTHROUGH_ENV variable name")
    missing = sorted(name for name in requested if name not in os.environ)
    if missing:
        raise ChipGrokError("requested provider environment variable is not set: " + ", ".join(missing))
    allowed = baseline | requested
    child = {name: value for name, value in os.environ.items() if name in allowed}
    secrets = [os.environ[name] for name in sorted(requested) if os.environ[name]]
    return child, secrets


def redact_text(text: str, secrets: list[str]) -> str:
    redacted = text
    for value in sorted(set(secrets), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED_SCOPED_CREDENTIAL]")
    # Defense in depth for common bearer output even when a wrapper obtained it
    # without direct environment passthrough.
    redacted = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]", redacted)
    if len(redacted) > MAX_RECEIPT_TEXT:
        redacted = redacted[:MAX_RECEIPT_TEXT] + "\n[TRUNCATED]"
    return redacted


def build_prompt(task: str, trusted: bool) -> str:
    trust_note = (
        "This process is explicitly operator-trusted and is not filesystem-sandboxed. "
        "The worktree separates changes, not host file access."
        if trusted
        else "An enforced Grok strict sandbox was requested; stop if it cannot initialize."
    )
    return f"""You are a reviewed coding worker in a dedicated git worktree.

Runtime boundary:
{trust_note}

Task:
{task.strip()}

Contract:
- Inspect the repository and its instructions before editing.
- Make the smallest defensible change that satisfies the task.
- Preserve existing behavior and unrelated user changes.
- Run focused tests or checks relevant to changed behavior.
- Do not commit, push, open a pull request, deploy, migrate, publish, or access credentials.
- Do not read files outside this repository.
- Do not use network tools unless the task explicitly requires source lookup.
- Finish with a concise summary of changed files, tests run, and blockers.
"""


def collect_changed_files(worktree: Path, base_head: str) -> list[str]:
    status = status_paths(git_status_raw(worktree))
    committed = run_command(
        ["git", "diff", "--name-only", "-z", f"{base_head}..HEAD"],
        cwd=worktree,
        check=False,
    )
    commit_paths = [item for item in committed.stdout.split("\0") if item] if committed.returncode == 0 else []
    return sorted(set(status + commit_paths))


def secret_leak_files(worktree: Path, changed_files: list[str], secrets: list[str]) -> list[str]:
    secret_bytes = [value.encode() for value in secrets if value]
    if not secret_bytes:
        return []
    leaks: list[str] = []
    root = worktree.resolve()
    for relative in changed_files:
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(value in data for value in secret_bytes):
            leaks.append(relative)
    return sorted(set(leaks))


def run_worker(
    repo: Path,
    task: str,
    keep: bool,
    *,
    trusted_worker: bool,
    sandbox_profile: str | None,
) -> dict[str, object]:
    if not trusted_worker and sandbox_profile != "strict":
        raise ChipGrokError(
            "refusing unsandboxed worker: pass --sandbox-profile strict, or explicitly acknowledge a fully trusted process with --trusted-worker"
        )
    command = grok_command()
    if sandbox_profile == "strict":
        if len(command) != 1 or Path(command[0]).name not in {"grok", "grok.exe"}:
            raise ChipGrokError("strict sandbox mode requires the direct Grok executable, not a pre-exec wrapper")
    child_env, secrets = worker_environment()
    receipt = prepare_worktree(repo)
    worktree = Path(str(receipt["worktree"]))
    token = str(receipt["run_token"])
    source_before = dict(receipt.pop("source_fingerprint"))
    base_head = str(receipt["base_head"])
    model = os.getenv("CHIP_GROK_MODEL", "").strip()
    if model:
        command.extend(["-m", model])
    if sandbox_profile:
        command.extend(["--sandbox", sandbox_profile])
    command.extend([
        "--cwd", str(worktree),
        "-p", build_prompt(task, trusted_worker),
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--no-subagents",
        "--disable-web-search",
        "--tools", "read_file,grep,list_dir,search_replace,run_terminal_cmd",
        "--max-turns", os.getenv("CHIP_GROK_MAX_TURNS", str(DEFAULT_MAX_TURNS)),
    ])
    timeout = int(os.getenv("CHIP_GROK_TIMEOUT", str(DEFAULT_TIMEOUT)))
    try:
        result = run_command(command, cwd=worktree, env=child_env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        receipt.update({
            "status": "blocked",
            "grok_exit_code": None,
            "model_alias": model or "wrapper/default",
            "changed_files": collect_changed_files(worktree, base_head),
            "diff_check_ok": False,
            "worker_result": "",
            "worker_error": f"Grok timed out after {timeout} seconds",
            "source_mutated": repo_fingerprint(repo) != source_before,
            "secret_leak_files": [],
            "kept": True,
        })
        return receipt

    changed = collect_changed_files(worktree, base_head)
    leaks = secret_leak_files(worktree, changed, secrets)
    source_after = repo_fingerprint(repo)
    source_mutated = source_after != source_before
    worker_head = git_text(worktree, "rev-parse", "HEAD")
    worker_committed = worker_head != base_head
    completed = result.returncode == 0 and not leaks and not source_mutated and not worker_committed
    receipt.update({
        "status": "completed" if completed else "blocked",
        "grok_exit_code": result.returncode,
        "model_alias": model or "wrapper/default",
        "trust_mode": "trusted-worker" if trusted_worker else f"sandbox:{sandbox_profile}",
        "changed_files": changed,
        "diff_check_ok": run_command(["git", "diff", "--check"], cwd=worktree, check=False).returncode == 0,
        "worker_result": redact_text(result.stdout.strip(), secrets),
        "worker_error": redact_text(result.stderr.strip(), secrets),
        "source_mutated": source_mutated,
        "worker_committed": worker_committed,
        "worker_head": worker_head,
        "secret_leak_files": leaks,
        "kept": keep or not completed or bool(changed),
    })
    if not receipt["kept"]:
        cleanup_worktree(repo, worktree, token, discard=False)
        receipt["worktree"] = None
    return receipt


def cleanup_worktree(repo: Path, worktree: Path, token: str, *, discard: bool) -> None:
    root = worktree_root().resolve()
    target = worktree.resolve()
    if root not in target.parents:
        raise ChipGrokError("refusing to clean a worktree outside CHIP_GROK_WORKTREE_ROOT")
    load_manifest(repo, target, token)
    dirty = bool(git_status_raw(target))
    if dirty and not discard:
        raise ChipGrokError("worktree has changes; pass --discard only after preserving or rejecting the diff")
    args = ["git", "worktree", "remove"]
    if discard:
        args.append("--force")
    args.append(str(target))
    run_command(args, cwd=repo)
    manifest_path(token).unlink()


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="create a detached worktree with an ownership receipt")
    prepare.add_argument("--repo", required=True)

    run = sub.add_parser("run", help="prepare a worktree and run Grok Build")
    run.add_argument("--repo", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--keep", action="store_true", help="keep an unchanged worktree too")
    boundary = run.add_mutually_exclusive_group()
    boundary.add_argument("--sandbox-profile", choices=["strict"])
    boundary.add_argument("--trusted-worker", action="store_true", help="acknowledge that Grok can access files available to this Unix user")

    clean = sub.add_parser("cleanup", help="remove an owned prepared worktree")
    clean.add_argument("--repo", required=True)
    clean.add_argument("--worktree", required=True)
    clean.add_argument("--run-token", required=True)
    clean.add_argument("--discard", action="store_true", help="force-remove an owned dirty worktree")
    return top


def main() -> int:
    args = parser().parse_args()
    try:
        repo = resolve_repo(args.repo)
        if args.command == "prepare":
            result = prepare_worktree(repo)
        elif args.command == "run":
            result = run_worker(
                repo,
                args.task,
                args.keep,
                trusted_worker=args.trusted_worker,
                sandbox_profile=args.sandbox_profile,
            )
        else:
            cleanup_worktree(repo, Path(args.worktree), args.run_token, discard=args.discard)
            result = {"status": "cleaned", "worktree": str(Path(args.worktree)), "run_token": args.run_token}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "blocked" else 1
    except (ChipGrokError, subprocess.TimeoutExpired, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
