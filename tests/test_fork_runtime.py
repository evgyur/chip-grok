from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from scripts.fork_contract import ContractError, verify_fork

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "chip_grok.py"
INSTALL = ROOT / "scripts" / "install-fork.sh"
UPSTREAM = "a" * 40
SOURCE = "b" * 40


class ForkRuntimeTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
        return repo

    def make_release(self, root: Path, *, fork: str, tag: str) -> tuple[Path, Path]:
        release = root / tag
        release.mkdir()
        binary = release / "grok"
        provenance = {
            "distribution": "chip",
            "version": "1.0.3",
            "fork_commit": fork,
            "upstream_commit": UPSTREAM,
            "upstream_source_rev": SOURCE,
            "auto_update": "externally-managed",
            "worker_contracts": [1],
        }
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"PROVENANCE={provenance!r}\n"
            "if sys.argv[1:] == ['version', '--json']:\n"
            " print(json.dumps(PROVENANCE)); raise SystemExit\n"
            "open('result.txt','w').write('worker ok\\n')\n"
            "print(json.dumps({'text':'done','stopReason':'end_turn'}))\n"
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        lock = release / "fork.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repository": "https://github.com/evgyur/grok-build",
                    "tag": tag,
                    "version": "1.0.3",
                    "binary_path": "grok",
                    "fork_commit": fork,
                    "upstream_commit": UPSTREAM,
                    "upstream_source_rev": SOURCE,
                    "platform": "linux-x86_64",
                    "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            )
            + "\n"
        )
        return binary, lock

    def verified_env(self, binary: Path, lock: Path, root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CHIP_GROK_UNVERIFIED_TEST_ONLY", None)
        env.update(
            {
                "CHIP_GROK_BIN": str(binary),
                "CHIP_GROK_LOCK_FILE": str(lock),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_TIMEOUT": "30",
            }
        )
        return env

    def test_verify_rejects_wrong_hash_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary, lock = self.make_release(root, fork="1" * 40, tag="chip-v1")
            self.assertEqual(verify_fork(binary, lock)["status"], "verified")
            payload = json.loads(lock.read_text())
            payload["sha256"] = "0" * 64
            lock.write_text(json.dumps(payload))
            with self.assertRaises(ContractError):
                verify_fork(binary, lock)

    def test_verified_runner_binds_binary_before_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            binary, lock = self.make_release(root, fork="2" * 40, tag="chip-v2")
            result = subprocess.run(
                ["python3", str(RUNNER), "run", "--repo", str(repo), "--task", "write result", "--trusted-worker", "--keep"],
                env=self.verified_env(binary, lock, root),
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["changed_files"], ["result.txt"])
            self.assertFalse((repo / "result.txt").exists())

    def test_invalid_preflight_blocks_before_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            binary, lock = self.make_release(root, fork="3" * 40, tag="chip-v3")
            payload = json.loads(lock.read_text())
            payload["fork_commit"] = "4" * 40
            lock.write_text(json.dumps(payload))
            env = self.verified_env(binary, lock, root)
            result = subprocess.run(
                ["python3", str(RUNNER), "run", "--repo", str(repo), "--task", "noop", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("preflight failed", json.loads(result.stdout)["error"])
            self.assertFalse((root / "worktrees").exists())

    def test_install_and_rollback_bind_binary_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_root = root / "installed"
            first = self.make_release(root, fork="5" * 40, tag="chip-v5")
            second = self.make_release(root, fork="6" * 40, tag="chip-v6")
            for binary, lock in (first, second):
                subprocess.run(
                    [str(INSTALL), "install", "--root", str(install_root), "--binary", str(binary), "--lock", str(lock)],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                )
            self.assertEqual(json.loads((install_root / "current" / "fork.lock.json").read_text())["tag"], "chip-v6")
            subprocess.run([str(INSTALL), "rollback", "--root", str(install_root)], check=True, stdout=subprocess.PIPE, text=True)
            self.assertEqual(json.loads((install_root / "current" / "fork.lock.json").read_text())["tag"], "chip-v5")


if __name__ == "__main__":
    unittest.main()
