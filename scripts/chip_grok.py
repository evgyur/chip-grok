#!/usr/bin/env python3
"""Run Grok Build as a reviewed coding worker in a dedicated git worktree."""

from __future__ import annotations

import argparse
import hashlib
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

try:
    from .fork_contract import ContractError, read_json, validate_lock, verify_fork_fd
except ImportError:
    from fork_contract import ContractError, read_json, validate_lock, verify_fork_fd

DEFAULT_MAX_TURNS = 60
DEFAULT_TIMEOUT = 1800
MAX_RECEIPT_TEXT = 200_000
MAX_SECRET_SCAN_BYTES = 256 * 1024 * 1024
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN_NAME = re.compile(r"^chip-grok-[0-9]+-[a-f0-9]{8}$")


class ChipGrokError(RuntimeError):
    pass


def terminate_process_group(pgid: int, grace: float = 0.5) -> None:
    """Terminate descendants left in a subprocess session/process group."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=pass_fds,
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
    finally:
        terminate_process_group(process.pid)
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


def git_text(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return run_command(["git", *args], cwd=repo, env=env).stdout.strip()


def git_status_raw(repo: Path, env: dict[str, str] | None = None) -> str:
    return run_command(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        env=env,
    ).stdout


def file_digest(path: Path) -> str:
    """Hash one filesystem object without following symlinks."""
    try:
        if path.is_symlink():
            return "SYMLINK:" + os.readlink(path)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return "MISSING"
    except OSError as exc:
        return f"ERROR:{type(exc).__name__}:{exc.errno}"


def ignored_state_digest(repo: Path) -> str:
    digest = hashlib.sha256()
    for relative in ignored_paths(repo):
        candidate = repo / relative
        digest.update(os.fsencode(relative))
        digest.update(b"\0")
        try:
            info = os.lstat(candidate)
            fields = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino)
            digest.update(":".join(str(value) for value in fields).encode())
            if candidate.is_symlink():
                digest.update(b"\0LINK\0")
                digest.update(os.fsencode(os.readlink(candidate)))
            elif candidate.is_file():
                digest.update(b"\0CONTENT\0")
                digest.update(file_digest(candidate).encode())
        except OSError as exc:
            digest.update(f"ERROR:{type(exc).__name__}:{exc.errno}".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def git_common_dir(repo: Path) -> Path:
    raw = Path(git_text(repo, "rev-parse", "--git-common-dir"))
    return raw.resolve() if raw.is_absolute() else (repo / raw).resolve()


def filesystem_metadata_digest(root: Path) -> str:
    """Fingerprint names and metadata without reading Git object contents."""
    digest = hashlib.sha256()
    if not root.exists():
        return "MISSING"
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        base = Path(current)
        for name in directories + files:
            path = base / name
            relative = path.relative_to(root)
            digest.update(os.fsencode(str(relative)))
            digest.update(b"\0")
            try:
                info = os.lstat(path)
                fields = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino)
                digest.update(":".join(str(value) for value in fields).encode())
                if path.is_symlink():
                    digest.update(b"\0LINK\0")
                    digest.update(os.fsencode(os.readlink(path)))
            except OSError as exc:
                digest.update(f"ERROR:{type(exc).__name__}:{exc.errno}".encode())
            digest.update(b"\0")
    return digest.hexdigest()


def refs_digest(repo: Path) -> str:
    result = run_command(
        ["git", "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"],
        cwd=repo,
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def repo_fingerprint(repo: Path) -> dict[str, str]:
    """Capture source checkout and shared Git metadata without writing Git objects."""
    # Git status may refresh cached stat data in the index. Run it before
    # hashing so our own observation cannot look like a source mutation.
    status_raw = git_status_raw(repo)
    head = git_text(repo, "rev-parse", "HEAD")
    git_dir_raw = Path(git_text(repo, "rev-parse", "--git-dir"))
    git_dir = git_dir_raw if git_dir_raw.is_absolute() else (repo / git_dir_raw)
    common_dir = git_common_dir(repo)
    index_raw = os.getenv("GIT_INDEX_FILE", "").strip()
    index_path = Path(index_raw).expanduser() if index_raw else git_dir / "index"
    if not index_path.is_absolute():
        index_path = repo / index_path
    return {
        "head": head,
        "index_digest": file_digest(index_path),
        "status_raw": status_raw,
        "ignored_state_digest": ignored_state_digest(repo),
        "refs_digest": refs_digest(repo),
        "objects_state_digest": filesystem_metadata_digest(common_dir / "objects"),
    }


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


def untracked_paths(raw: str) -> list[str]:
    """Return exact untracked files from porcelain-v1 -z output."""
    return sorted(
        item[3:]
        for item in raw.split("\0")
        if item and item[:2] == "??" and len(item) >= 4
    )


def ignored_paths(repo: Path, env: dict[str, str] | None = None) -> list[str]:
    result = run_command(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=repo,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise ChipGrokError("failed to enumerate ignored worker outputs")
    return sorted(item for item in result.stdout.split("\0") if item)


def worktree_root() -> Path:
    configured = os.getenv("CHIP_GROK_WORKTREE_ROOT", "").strip()
    raw = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "chip-grok-worktrees"
    if raw.is_symlink():
        raise ChipGrokError("worktree root must not be a symlink")
    root = raw.resolve()
    if root.exists():
        info = root.stat()
        if not root.is_dir():
            raise ChipGrokError("worktree root exists but is not a directory")
        if info.st_uid != os.getuid():
            raise ChipGrokError("worktree root must be owned by the current user")
        if info.st_mode & 0o077:
            raise ChipGrokError("existing worktree root must already be private (mode 0700)")
    else:
        root.mkdir(parents=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            try:
                root.rmdir()
            except OSError:
                pass
            raise
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
    """Create an independent detached clone; keep the legacy receipt key `worktree`."""
    source = repo_fingerprint(repo)
    if source["status_raw"]:
        raise ChipGrokError("source repository must be clean before preparing a worker clone")
    head = source["head"]
    branch = git_text(repo, "branch", "--show-current") or "DETACHED"
    token = f"chip-grok-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    destination = worktree_root() / token
    clone = run_command(
        ["git", "clone", "--no-hardlinks", "--no-checkout", "--quiet", str(repo), str(destination)],
        check=False,
    )
    if clone.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise ChipGrokError("failed to create independent worker clone: " + (clone.stderr or clone.stdout).strip())
    checkout = run_command(["git", "checkout", "--detach", "--quiet", head], cwd=destination, check=False)
    if checkout.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise ChipGrokError("failed to checkout worker clone at base HEAD")
    manifest = {
        "format": 1,
        "run_token": token,
        "repo": str(repo),
        "worktree": str(destination),
        "base_head": head,
        "created_at": int(time.time()),
    }
    try:
        if repo_fingerprint(repo) != source:
            raise ChipGrokError("source repository changed while preparing the worker clone")
        write_manifest(manifest)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        **manifest,
        "base_branch": branch,
        "source_dirty": False,
        "source_status": [],
        "source_fingerprint": source,
    }


def fork_lock_path() -> Path:
    configured = os.getenv("CHIP_GROK_LOCK_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "lib" / "chip-grok" / "current" / "fork.lock.json"


def grok_command() -> tuple[list[str], int | None]:
    configured = os.getenv("CHIP_GROK_BIN", "").strip()
    if os.getenv("CHIP_GROK_UNVERIFIED_TEST_ONLY") == "1":
        parts = shlex.split(configured or "grok")
        if not parts:
            raise ChipGrokError("CHIP_GROK_BIN is empty")
        executable = shutil.which(parts[0]) if not Path(parts[0]).is_absolute() else parts[0]
        if not executable or not Path(executable).exists():
            raise ChipGrokError("Grok executable or wrapper was not found")
        parts[0] = str(executable)
        return parts, None

    lock_path = fork_lock_path()
    try:
        lock = validate_lock(read_json(lock_path))
    except ContractError as exc:
        raise ChipGrokError(f"verified fork preflight failed: {exc}") from exc
    binary = Path(configured).expanduser().resolve() if configured else lock_path.parent / lock["binary_path"]
    if binary.resolve() != (lock_path.parent / lock["binary_path"]).resolve():
        raise ChipGrokError("verified fork binary path does not match the active lock")
    if configured and len(shlex.split(configured)) != 1:
        raise ChipGrokError("verified fork mode requires the direct Grok executable")
    binary_fd: int | None = None
    try:
        binary_fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW)
        verify_fork_fd(binary_fd, binary, lock_path)
    except (OSError, ContractError) as exc:
        try:
            if binary_fd is not None:
                os.close(binary_fd)
        except OSError:
            pass
        raise ChipGrokError(f"verified fork preflight failed: {exc}") from exc
    return [f"/proc/self/fd/{binary_fd}"], binary_fd


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


def redact_paths(paths: list[str], secrets: list[str]) -> list[str]:
    return [redact_text(path, secrets) for path in paths]


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


def collect_changed_files(
    worktree: Path,
    base_head: str,
    git_env: dict[str, str] | None = None,
) -> list[str]:
    status = status_paths(git_status_raw(worktree, env=git_env))
    committed = run_command(
        ["git", "diff", "--name-only", "-z", f"{base_head}..HEAD"],
        cwd=worktree,
        env=git_env,
        check=False,
    )
    commit_paths = [item for item in committed.stdout.split("\0") if item] if committed.returncode == 0 else []
    return sorted(set(status + commit_paths))


def worker_commit_created(worktree: Path, git_env: dict[str, str] | None = None) -> bool:
    result = run_command(
        ["git", "fsck", "--unreachable", "--no-reflogs"],
        cwd=worktree,
        env=git_env,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).splitlines()
    return any(line.startswith(("unreachable commit ", "dangling commit ")) for line in output)


def normalize_worker_git(
    worktree: Path,
    base_head: str,
    refs_before: str,
    git_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    worker_head = git_text(worktree, "rev-parse", "HEAD", env=git_env)
    refs_after = refs_digest(worktree)
    committed = (
        worker_head != base_head
        or refs_after != refs_before
        or worker_commit_created(worktree, git_env)
    )
    reset = run_command(
        ["git", "reset", "--mixed", base_head],
        cwd=worktree,
        env=git_env,
        check=False,
    )
    if reset.returncode != 0:
        raise ChipGrokError("failed to materialize worker Git state for review")
    return worker_head, committed


def untracked_whitespace_ok(worktree: Path, paths: list[str]) -> bool:
    """Streaming equivalent of the relevant `git diff --check` whitespace rule."""
    root = worktree.resolve()
    for relative in paths:
        candidate = root / relative
        if candidate.is_symlink():
            continue
        if not candidate.is_file():
            return False
        try:
            previous = b""
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    data = previous + chunk
                    if re.search(rb"[ \t]\r?\n", data):
                        return False
                    previous = data[-2:]
            if previous.endswith((b" ", b"\t")):
                return False
        except (OSError, MemoryError):
            return False
    return True


def secret_leak_files(
    worktree: Path,
    candidate_files: list[str],
    secrets: list[str],
) -> tuple[list[str], bool]:
    secret_bytes = [value.encode() for value in secrets if value]
    if not secret_bytes:
        return [], True
    leaks: list[str] = []
    scanned = 0
    overlap = max(len(value) for value in secret_bytes) - 1
    root = worktree.resolve()
    for relative in sorted(set(candidate_files)):
        relative_bytes = os.fsencode(relative)
        if any(value in relative_bytes for value in secret_bytes):
            leaks.append(relative)
        unresolved = root / relative
        if unresolved.is_symlink():
            try:
                data = os.readlink(unresolved).encode()
            except OSError:
                return sorted(set(leaks)), False
            scanned += len(data)
            if scanned > MAX_SECRET_SCAN_BYTES:
                return sorted(set(leaks)), False
            if any(value in data for value in secret_bytes):
                leaks.append(relative)
            continue
        path = unresolved.resolve()
        if root not in path.parents or not path.is_file():
            return sorted(set(leaks)), False
        try:
            previous = b""
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    scanned += len(chunk)
                    if scanned > MAX_SECRET_SCAN_BYTES:
                        return sorted(set(leaks)), False
                    data = previous + chunk
                    if any(value in data for value in secret_bytes):
                        leaks.append(relative)
                        break
                    previous = data[-overlap:] if overlap > 0 else b""
        except OSError:
            return sorted(set(leaks)), False
    return sorted(set(leaks)), True


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
    child_env, secrets = worker_environment()
    try:
        timeout = int(os.getenv("CHIP_GROK_TIMEOUT", str(DEFAULT_TIMEOUT)))
        max_turns = int(os.getenv("CHIP_GROK_MAX_TURNS", str(DEFAULT_MAX_TURNS)))
    except ValueError as exc:
        raise ChipGrokError("CHIP_GROK_TIMEOUT and CHIP_GROK_MAX_TURNS must be integers") from exc
    if timeout <= 0 or max_turns <= 0:
        raise ChipGrokError("CHIP_GROK_TIMEOUT and CHIP_GROK_MAX_TURNS must be positive")
    command, binary_fd = grok_command()
    if sandbox_profile == "strict":
        if binary_fd is None and (len(command) != 1 or Path(command[0]).name not in {"grok", "grok.exe"}):
            raise ChipGrokError("strict sandbox mode requires the direct Grok executable, not a pre-exec wrapper")
    receipt = prepare_worktree(repo)
    worktree = Path(str(receipt["worktree"]))
    token = str(receipt["run_token"])
    source_before = dict(receipt.pop("source_fingerprint"))
    base_head = str(receipt["base_head"])
    worker_env = child_env
    refs_before = refs_digest(worktree)
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
        "--max-turns", str(max_turns),
    ])
    try:
        result = run_command(
            command,
            cwd=worktree,
            env=worker_env,
            timeout=timeout,
            check=False,
            pass_fds=() if binary_fd is None else (binary_fd,),
        )
    except subprocess.TimeoutExpired:
        if binary_fd is not None:
            os.close(binary_fd)
        worker_head, worker_committed = normalize_worker_git(
            worktree, base_head, refs_before, worker_env
        )
        changed = sorted(set(collect_changed_files(worktree, base_head) + ignored_paths(worktree)))
        leaks, scan_complete = secret_leak_files(worktree, changed, secrets)
        receipt.update({
            "status": "blocked",
            "grok_exit_code": None,
            "model_alias": model or "wrapper/default",
            "changed_files": redact_paths(changed, secrets),
            "diff_check_ok": False,
            "worker_result": "",
            "worker_error": f"Grok timed out after {timeout} seconds",
            "source_mutated": repo_fingerprint(repo) != source_before,
            "worker_committed": worker_committed,
            "worker_head": worker_head,
            "clone_isolated": True,
            "secret_leak_files": redact_paths(leaks, secrets),
            "secret_scan_complete": scan_complete,
            "kept": True,
        })
        return receipt

    if binary_fd is not None:
        os.close(binary_fd)

    worker_head, worker_committed = normalize_worker_git(
        worktree, base_head, refs_before, worker_env
    )
    changed = sorted(set(collect_changed_files(worktree, base_head) + ignored_paths(worktree)))
    leaks, scan_complete = secret_leak_files(worktree, changed, secrets)
    source_after = repo_fingerprint(repo)
    source_mutated = source_after != source_before
    diff_check = run_command(["git", "diff", "--check", base_head], cwd=worktree, check=False)
    worktree_status = git_status_raw(worktree)
    untracked = untracked_paths(worktree_status)
    untracked_check_ok = untracked_whitespace_ok(worktree, untracked)
    diff_check_ok = diff_check.returncode == 0 and untracked_check_ok
    completed = result.returncode == 0 and diff_check_ok and scan_complete and not leaks and not source_mutated and not worker_committed
    receipt.update({
        "status": "completed" if completed else "blocked",
        "grok_exit_code": result.returncode,
        "model_alias": model or "wrapper/default",
        "trust_mode": "trusted-worker" if trusted_worker else f"sandbox:{sandbox_profile}",
        "changed_files": redact_paths(changed, secrets),
        "diff_check_ok": diff_check_ok,
        "worker_result": redact_text(result.stdout.strip(), secrets),
        "worker_error": redact_text(result.stderr.strip(), secrets),
        "source_mutated": source_mutated,
        "worker_committed": worker_committed,
        "worker_head": worker_head,
        "clone_isolated": True,
        "secret_leak_files": redact_paths(leaks, secrets),
        "secret_scan_complete": scan_complete,
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
    manifest = load_manifest(repo, target, token)
    worktree_head = git_text(target, "rev-parse", "HEAD")
    committed = worktree_head != str(manifest.get("base_head", ""))
    dirty = bool(git_status_raw(target)) or bool(ignored_paths(target)) or committed
    if dirty and not discard:
        reason = "commits or changes" if committed else "changes"
        raise ChipGrokError(
            f"worktree has {reason}; pass --discard only after preserving or rejecting the diff"
        )
    if not target.joinpath(".git").is_dir():
        raise ChipGrokError("owned worker clone is not an independent Git repository")
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ChipGrokError("failed to remove owned worker clone") from exc
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
