#!/usr/bin/env python3
"""Run Grok Build as a bounded coding worker in an isolated git worktree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

DEFAULT_MAX_TURNS = 60
DEFAULT_TIMEOUT = 1800
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ChipGrokError(f"command failed ({result.returncode}): {detail}")
    return result


def resolve_repo(raw: str) -> Path:
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ChipGrokError("repository path does not exist or is not a directory")
    root = run_command(["git", "rev-parse", "--show-toplevel"], cwd=repo).stdout.strip()
    resolved = Path(root).resolve()
    if resolved != repo:
        repo = resolved
    return repo


def git_text(repo: Path, *args: str) -> str:
    return run_command(["git", *args], cwd=repo).stdout.strip()


def worktree_root() -> Path:
    configured = os.getenv("CHIP_GROK_WORKTREE_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = Path(tempfile.gettempdir()) / "chip-grok-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def prepare_worktree(repo: Path) -> dict[str, object]:
    head = git_text(repo, "rev-parse", "HEAD")
    branch = git_text(repo, "branch", "--show-current") or "DETACHED"
    status = git_text(repo, "status", "--short")
    token = f"chip-grok-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    destination = worktree_root() / token
    run_command(
        ["git", "worktree", "add", "--detach", str(destination), head],
        cwd=repo,
    )
    return {
        "repo": str(repo),
        "base_head": head,
        "base_branch": branch,
        "source_dirty": bool(status),
        "source_status": status.splitlines() if status else [],
        "worktree": str(destination),
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


def worker_environment() -> dict[str, str]:
    """Build a minimal child environment plus explicitly scoped provider vars."""
    baseline = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
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
        raise ChipGrokError(
            "requested provider environment variable is not set: " + ", ".join(missing)
        )
    allowed = baseline | requested
    return {name: value for name, value in os.environ.items() if name in allowed}


def build_prompt(task: str) -> str:
    return f"""You are a bounded coding worker inside an isolated git worktree.

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


def collect_changed_files(worktree: Path) -> list[str]:
    result = run_command(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=worktree,
    ).stdout
    changed: list[str] = []
    for item in result.split("\0"):
        if not item:
            continue
        path = item[3:] if len(item) >= 4 else item
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(path)
    return sorted(set(changed))


def run_worker(repo: Path, task: str, keep: bool) -> dict[str, object]:
    command = grok_command()
    child_env = worker_environment()
    receipt = prepare_worktree(repo)
    worktree = Path(str(receipt["worktree"]))
    model = os.getenv("CHIP_GROK_MODEL", "").strip()
    if model:
        command.extend(["-m", model])
    command.extend(
        [
            "--cwd",
            str(worktree),
            "-p",
            build_prompt(task),
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--no-subagents",
            "--disable-web-search",
            "--tools",
            "read_file,grep,list_dir,search_replace,run_terminal_cmd",
            "--max-turns",
            os.getenv("CHIP_GROK_MAX_TURNS", str(DEFAULT_MAX_TURNS)),
        ]
    )
    timeout = int(os.getenv("CHIP_GROK_TIMEOUT", str(DEFAULT_TIMEOUT)))
    try:
        result = run_command(
            command,
            cwd=worktree,
            env=child_env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        receipt.update(
            {
                "status": "blocked",
                "grok_exit_code": None,
                "model_alias": model or "wrapper/default",
                "changed_files": collect_changed_files(worktree),
                "diff_check_ok": False,
                "worker_result": "",
                "worker_error": f"Grok timed out after {timeout} seconds",
                "kept": True,
            }
        )
        return receipt
    receipt.update(
        {
            "status": "completed" if result.returncode == 0 else "blocked",
            "grok_exit_code": result.returncode,
            "model_alias": model or "wrapper/default",
            "changed_files": collect_changed_files(worktree),
            "diff_check_ok": run_command(
                ["git", "diff", "--check"], cwd=worktree, check=False
            ).returncode
            == 0,
            "worker_result": result.stdout.strip(),
            "worker_error": result.stderr.strip(),
            "kept": keep or result.returncode != 0,
        }
    )
    if not receipt["kept"] and not receipt["changed_files"]:
        cleanup_worktree(repo, worktree)
        receipt["worktree"] = None
    return receipt


def cleanup_worktree(repo: Path, worktree: Path) -> None:
    root = worktree_root().resolve()
    target = worktree.resolve()
    if root not in target.parents:
        raise ChipGrokError("refusing to clean a worktree outside CHIP_GROK_WORKTREE_ROOT")
    run_command(["git", "worktree", "remove", "--force", str(target)], cwd=repo)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="create an isolated detached worktree")
    prepare.add_argument("--repo", required=True)

    run = sub.add_parser("run", help="prepare a worktree and run Grok Build")
    run.add_argument("--repo", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--keep", action="store_true", help="keep an unchanged worktree too")

    clean = sub.add_parser("cleanup", help="remove a prepared worktree")
    clean.add_argument("--repo", required=True)
    clean.add_argument("--worktree", required=True)
    return top


def main() -> int:
    args = parser().parse_args()
    try:
        repo = resolve_repo(args.repo)
        if args.command == "prepare":
            result = prepare_worktree(repo)
        elif args.command == "run":
            result = run_worker(repo, args.task, args.keep)
        else:
            cleanup_worktree(repo, Path(args.worktree))
            result = {"status": "cleaned", "worktree": str(Path(args.worktree))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "blocked" else 1
    except (ChipGrokError, subprocess.TimeoutExpired, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
