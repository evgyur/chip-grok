from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shlex
import stat
import subprocess
import tempfile
import time
import unittest

from scripts.chip_grok import repo_fingerprint, secret_leak_files, status_paths, untracked_whitespace_ok

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "chip_grok.py"


class ChipGrokTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_unverified_test_mode = os.environ.get("CHIP_GROK_UNVERIFIED_TEST_ONLY")
        os.environ["CHIP_GROK_UNVERIFIED_TEST_ONLY"] = "1"

    def tearDown(self) -> None:
        if self._old_unverified_test_mode is None:
            os.environ.pop("CHIP_GROK_UNVERIFIED_TEST_ONLY", None)
        else:
            os.environ["CHIP_GROK_UNVERIFIED_TEST_ONLY"] = self._old_unverified_test_mode

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

    def make_fake_grok(self, root: Path) -> Path:
        fake = root / "fake-grok"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import json, os\n"
            "cwd = Path(os.getcwd())\n"
            "(cwd / 'result.txt').write_text('worker ok\\n')\n"
            "print(json.dumps({'text': 'done', 'stopReason': 'end_turn'}))\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def test_invalid_runtime_limits_fail_before_worktree_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            worktrees = root / "worktrees"
            env = os.environ | {
                "CHIP_GROK_BIN": str(self.make_fake_grok(root)),
                "CHIP_GROK_WORKTREE_ROOT": str(worktrees),
                "CHIP_GROK_TIMEOUT": "not-an-int",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "noop", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be integers", json.loads(result.stdout)["error"])
            self.assertFalse(worktrees.exists())

    def test_existing_shared_worktree_root_is_not_chmodded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            worktrees = root / "shared-worktrees"
            worktrees.mkdir(mode=0o755)
            before = stat.S_IMODE(worktrees.stat().st_mode)
            result = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(worktrees)},
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must already be private", json.loads(result.stdout)["error"])
            self.assertEqual(stat.S_IMODE(worktrees.stat().st_mode), before)

    def test_symlinked_existing_worktree_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            real = root / "real-root"
            real.mkdir(mode=0o700)
            link = root / "linked-root"
            link.symlink_to(real, target_is_directory=True)
            result = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(link)},
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be a symlink", json.loads(result.stdout)["error"])

    def test_new_worktree_root_is_private_even_with_permissive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            worktrees = root / "new-worktrees"
            command = (
                "umask 000; exec python3 "
                + shlex.quote(str(SCRIPT))
                + " prepare --repo "
                + shlex.quote(str(repo))
            )
            result = subprocess.run(
                ["bash", "-c", command],
                env=os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(worktrees)},
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(stat.S_IMODE(worktrees.stat().st_mode), 0o700)
            receipt = json.loads(result.stdout)
            removed = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", receipt["worktree"], "--run-token", receipt["run_token"]],
                env=os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(worktrees)},
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(json.loads(removed.stdout)["status"], "cleaned")

    def test_dirty_source_repo_is_refused_before_worktree_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / "dirty.txt").write_text("do not absorb\n")
            worktrees = root / "worktrees"
            result = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(worktrees)},
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source repository must be clean", json.loads(result.stdout)["error"])
            self.assertFalse(worktrees.exists())

    def test_prepare_creates_independent_detached_clean_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees")}
            result = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            worktree = Path(receipt["worktree"])
            self.assertTrue(worktree.is_dir())
            self.assertTrue(worktree.joinpath(".git").is_dir())
            self.assertFalse(worktree.joinpath(".git", "objects", "info", "alternates").exists())
            self.assertEqual(receipt["base_head"], subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip())
            self.assertFalse(receipt["source_dirty"])
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=worktree, text=True).strip()
            self.assertEqual(branch, "")

    def test_run_uses_fake_worker_and_leaves_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = self.make_fake_grok(root)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_MODEL": "test-model",
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_TIMEOUT": "30",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "write result", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["changed_files"], ["result.txt"])
            self.assertEqual(receipt["model_alias"], "test-model")
            self.assertFalse((repo / "result.txt").exists())
            self.assertTrue(Path(receipt["worktree"]).joinpath("result.txt").exists())

    def test_worker_environment_passes_only_explicit_provider_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import json, os\n"
                "Path('env.json').write_text(json.dumps({'scoped_present': 'SCOPED_TEST_KEY' in os.environ, 'unrelated_present': 'UNRELATED_GATEWAY_SECRET' in os.environ}))\n"
                "print(json.dumps({'text': 'done'}))\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "scoped-value",
                "UNRELATED_GATEWAY_SECRET": "must-not-pass",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "inspect env", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            child = json.loads(Path(receipt["worktree"]).joinpath("env.json").read_text())
            self.assertTrue(child["scoped_present"])
            self.assertFalse(child["unrelated_present"])

    def test_missing_worker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {
                "CHIP_GROK_BIN": str(root / "missing-grok"),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "noop", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["status"], "blocked")

    def test_cleanup_refuses_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees")}
            prepared = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(prepared.stdout)
            result = subprocess.run(
                [
                    "python3", str(SCRIPT), "cleanup",
                    "--repo", str(repo),
                    "--worktree", str(repo),
                    "--run-token", receipt["run_token"],
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing", json.loads(result.stdout)["error"])

    def test_unsandboxed_run_requires_explicit_trusted_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = self.make_fake_grok(root)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "noop"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing unsandboxed worker", json.loads(result.stdout)["error"])

    def test_strict_mode_refuses_pre_exec_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = self.make_fake_grok(root)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "noop", "--sandbox-profile", "strict"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("direct Grok executable", json.loads(result.stdout)["error"])

    def test_scoped_credential_is_redacted_and_changed_file_leak_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "secret = os.environ['SCOPED_TEST_KEY']\n"
                "Path('leak.txt').write_text(secret)\n"
                "print(secret)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "synthetic-scoped-value-123",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "leak", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertNotIn("synthetic-scoped-value-123", result.stdout)
            self.assertIn("[REDACTED_SCOPED_CREDENTIAL]", receipt["worker_result"])
            self.assertEqual(receipt["secret_leak_files"], ["leak.txt"])

    def test_source_escape_is_detected_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os, subprocess\n"
                "source = Path(os.environ['TEST_SOURCE_REPO'])\n"
                "(source / 'SOURCE_ESCAPED.txt').write_text('escaped\\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "TEST_SOURCE_REPO",
                "TEST_SOURCE_REPO": str(repo),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "escape", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(receipt["source_mutated"])
            self.assertEqual(receipt["status"], "blocked")

    def test_cleanup_refuses_committed_worktree_without_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees")}
            prepared = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(prepared.stdout)
            worktree = Path(receipt["worktree"])
            subprocess.run(
                ["git", "-c", "user.name=Worker", "-c", "user.email=worker@example.invalid", "commit", "--allow-empty", "-qm", "worker commit"],
                cwd=worktree,
                check=True,
            )
            refused = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(worktree), "--run-token", receipt["run_token"]],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("commits or changes", json.loads(refused.stdout)["error"])
            self.assertTrue(worktree.exists())
            removed = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(worktree), "--run-token", receipt["run_token"], "--discard"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(json.loads(removed.stdout)["status"], "cleaned")

    def test_cleanup_requires_owned_receipt_and_refuses_dirty_without_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {"CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees")}
            prepared = subprocess.run(
                ["python3", str(SCRIPT), "prepare", "--repo", str(repo)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(prepared.stdout)
            worktree = Path(receipt["worktree"])
            (worktree / "dirty.txt").write_text("dirty\n")
            wrong = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(worktree), "--run-token", "chip-grok-12345678-deadbeef"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertTrue(worktree.exists())
            refused = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(worktree), "--run-token", receipt["run_token"]],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("has changes", json.loads(refused.stdout)["error"])
            self.assertTrue(worktree.exists())
            removed = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(worktree), "--run-token", receipt["run_token"], "--discard"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(json.loads(removed.stdout)["status"], "cleaned")
            self.assertFalse(worktree.exists())

    def test_proxy_credentials_do_not_pass_without_explicit_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "print(json.dumps({'proxy_present': 'HTTP_PROXY' in os.environ or 'HTTPS_PROXY' in os.environ}))\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "HTTP_PROXY": "http://proxy-user:synthetic-proxy-password@example.invalid:8080",
                "HTTPS_PROXY": "http://proxy-user:synthetic-proxy-password@example.invalid:8080",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "inspect proxy", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            self.assertNotIn("synthetic-proxy-password", result.stdout)
            self.assertIn('"proxy_present": false', receipt["worker_result"])

    def test_worker_commit_is_blocked_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import subprocess\n"
                "Path('committed.txt').write_text('preserve me\\n')\n"
                "subprocess.run(['git','add','committed.txt'], check=True)\n"
                "subprocess.run(['git','-c','user.name=Worker','-c','user.email=worker@example.invalid','commit','-qm','worker commit'], check=True)\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "commit", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertTrue(receipt["worker_committed"])
            self.assertIn("committed.txt", receipt["changed_files"])
            self.assertTrue(Path(receipt["worktree"]).joinpath("committed.txt").exists())

    def test_successful_worker_does_not_leave_background_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import subprocess\n"
                "child = subprocess.Popen(['sleep','120'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
                "Path('child.pid').write_text(str(child.pid))\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "background child", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0)
            pid = int(Path(receipt["worktree"]).joinpath("child.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_timeout_kills_worker_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import subprocess, time\n"
                "child = subprocess.Popen(['sleep','120'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
                "Path('child.pid').write_text(str(child.pid))\n"
                "time.sleep(120)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_TIMEOUT": "1",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "timeout", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            pid = int(Path(receipt["worktree"]).joinpath("child.pid").read_text())
            time.sleep(0.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_source_ignored_file_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / ".gitignore").write_text(".env\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ignore env"], cwd=repo, check=True)
            source_env = repo / ".env"
            source_env.write_text("original-value\n")
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os, subprocess\n"
                "source = Path(os.environ['TEST_SOURCE_REPO'])\n"
                "target = source / '.env'\n"
                "stamp = target.stat()\n"
                "target.write_text('corruptd-value\\n')\n"
                "os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "TEST_SOURCE_REPO",
                "TEST_SOURCE_REPO": str(repo),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "mutate ignored source", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(receipt["source_mutated"])

    def test_source_head_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os, subprocess\n"
                "source = Path(os.environ['TEST_SOURCE_REPO'])\n"
                "env = dict(os.environ)\n"
                "[env.pop(name, None) for name in ('GIT_DIR','GIT_WORK_TREE','GIT_ALTERNATE_OBJECT_DIRECTORIES')]\n"
                "subprocess.run(['git','-c','user.name=Worker','-c','user.email=worker@example.invalid','commit','--allow-empty','-qm','source mutation'], cwd=source, env=env, check=True)\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "TEST_SOURCE_REPO",
                "TEST_SOURCE_REPO": str(repo),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "mutate source head", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(receipt["source_mutated"])

    def test_installer_self_update_from_active_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            first = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install.sh")],
                env=os.environ | {"HERMES_HOME": str(home)},
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            target = home / "skills" / "chip-grok"
            self.assertTrue(target.is_dir(), first.stdout)
            second = subprocess.run(
                ["bash", str(target / "scripts" / "install.sh")],
                env=os.environ | {"HERMES_HOME": str(home)},
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertTrue(target.joinpath("SKILL.md").is_file(), second.stdout)
            self.assertTrue(any((home / "skill-backups").glob("chip-grok.*")))

    def test_fingerprint_does_not_write_git_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            # Stage content whose tree is not yet stored; `git write-tree` would
            # create a new object here and mutate the repository during review.
            (repo / "README.md").write_text("staged but uncommitted\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            before = subprocess.check_output(["git", "count-objects", "-v"], cwd=repo, text=True)
            first = repo_fingerprint(repo)
            second = repo_fingerprint(repo)
            after = subprocess.check_output(["git", "count-objects", "-v"], cwd=repo, text=True)
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertIn("index_digest", first)

    def test_whitespace_error_blocks_completed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "Path('bad.txt').write_text('trailing whitespace   \\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "bad whitespace", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertFalse(receipt["diff_check_ok"])
            self.assertTrue(Path(receipt["worktree"]).joinpath("bad.txt").exists())

    def test_nested_untracked_whitespace_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "Path('nested').mkdir()\n"
                "Path('nested/bad.txt').write_text('trailing whitespace   \\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "bad nested whitespace", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nested/bad.txt", receipt["changed_files"])
            self.assertFalse(receipt["diff_check_ok"])

    def test_broken_untracked_symlink_is_allowed_when_no_secret_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "os.symlink('missing-public-target', 'public-link')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "public symlink", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "completed")
            self.assertTrue(receipt["diff_check_ok"])

    def test_large_sparse_untracked_whitespace_check_is_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "large.bin"
            with large.open("wb") as handle:
                handle.truncate(128 * 1024 * 1024)
            self.assertTrue(untracked_whitespace_ok(root, ["large.bin"]))

    def test_streaming_secret_scan_detects_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            needle = "boundary-credential-value"
            payload = b"x" * (1024 * 1024 - 5) + needle.encode() + b"end"
            (root / "large.bin").write_bytes(payload)
            leaks, complete = secret_leak_files(root, ["large.bin"], [needle])
            self.assertTrue(complete)
            self.assertEqual(leaks, ["large.bin"])

    def test_ignored_output_is_kept_and_cleanup_requires_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / ".gitignore").write_text("cache.bin\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ignore cache"], cwd=repo, check=True)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "Path('cache.bin').write_text('artifact\\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            worktree_root = root / "worktrees"
            worktree_root.mkdir(mode=0o700)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(worktree_root),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "ignored artifact", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["changed_files"], ["cache.bin"])
            self.assertTrue(receipt["kept"])
            self.assertTrue(Path(receipt["worktree"]).joinpath("cache.bin").exists())
            refused = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", receipt["worktree"], "--run-token", receipt["run_token"]],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("worktree has changes", json.loads(refused.stdout)["error"])

    def test_ignored_secret_file_blocks_completed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / ".gitignore").write_text(".env\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ignore env"], cwd=repo, check=True)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "Path('.env').write_text(('TO' + 'KEN=') + os.environ['SCOPED_TEST_KEY'] + '\\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            worktree_root = root / "worktrees"
            worktree_root.mkdir(mode=0o700)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(worktree_root),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "ignored-scoped-secret",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "ignored leak", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["changed_files"], [".env"])
            self.assertEqual(receipt["secret_leak_files"], [".env"])

    def test_secret_in_filename_is_detected_and_redacted_from_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "Path('leak-' + os.environ['SCOPED_TEST_KEY'] + '.txt').write_text('public content\\n')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "filename-credential-value",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "filename leak", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("filename-credential-value", result.stdout)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["changed_files"], ["leak-[REDACTED_SCOPED_CREDENTIAL].txt"])
            self.assertEqual(receipt["secret_leak_files"], ["leak-[REDACTED_SCOPED_CREDENTIAL].txt"])

    def test_transient_worker_commit_is_blocked_without_shared_git_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            before = subprocess.check_output(
                ["git", "count-objects", "-v"], cwd=repo, text=True
            )
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import subprocess\n"
                "base = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()\n"
                "Path('transient.txt').write_text('transient credential payload\\n')\n"
                "subprocess.run(['git','add','transient.txt'], check=True)\n"
                "subprocess.run(['git','-c','user.name=Worker','-c','user.email=worker@example.invalid','commit','-qm','transient'], check=True)\n"
                "subprocess.run(['git','tag','transient-tag'], check=True)\n"
                "subprocess.run(['git','reset','--hard',base], check=True)\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "transient commit", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertTrue(receipt["worker_committed"])
            self.assertEqual(before, subprocess.check_output(["git", "count-objects", "-v"], cwd=repo, text=True))
            unreachable = subprocess.check_output(
                ["git", "fsck", "--unreachable", "--no-reflogs"],
                cwd=repo,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(unreachable, "")

    def test_worker_tag_is_private_and_does_not_mutate_source_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess\n"
                "subprocess.run(['git','tag','worker-created-tag'], check=True)\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "tag", "--trusted-worker"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertTrue(receipt["worker_committed"])
            self.assertTrue(Path(receipt["worktree"]).is_dir())
            self.assertEqual(subprocess.check_output(["git", "tag", "--list"], cwd=repo, text=True), "")

    def test_symlink_target_secret_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "os.symlink('prefix-' + os.environ['SCOPED_TEST_KEY'], 'leak-link')\n"
                "print('done')\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "scoped-symlink-secret",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "symlink leak", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["secret_leak_files"], ["leak-link"])

    def test_timeout_receipt_reports_worker_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import subprocess, time\n"
                "Path('committed-before-timeout.txt').write_text('preserve\\n')\n"
                "subprocess.run(['git','add','committed-before-timeout.txt'], check=True)\n"
                "subprocess.run(['git','-c','user.name=Worker','-c','user.email=worker@example.invalid','commit','-qm','commit before timeout'], check=True)\n"
                "time.sleep(120)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
                "CHIP_GROK_TIMEOUT": "1",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "commit then timeout", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(receipt["worker_committed"])
            self.assertIn("committed-before-timeout.txt", receipt["changed_files"])
            self.assertNotEqual(receipt["worker_head"], receipt["base_head"])
            self.assertEqual(receipt["secret_leak_files"], [])

    def test_timeout_receipt_scans_ignored_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / ".gitignore").write_text(".env\n")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ignore env"], cwd=repo, check=True)
            fake = root / "fake-grok"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os, time\n"
                "Path('.env').write_text(('TO' + 'KEN=') + os.environ['SCOPED_TEST_KEY'] + '\\n')\n"
                "time.sleep(120)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            worktree_root = root / "worktrees"
            worktree_root.mkdir(mode=0o700)
            env = os.environ | {
                "CHIP_GROK_BIN": str(fake),
                "CHIP_GROK_WORKTREE_ROOT": str(worktree_root),
                "CHIP_GROK_PASSTHROUGH_ENV": "SCOPED_TEST_KEY",
                "SCOPED_TEST_KEY": "timeout-ignored-secret",
                "CHIP_GROK_TIMEOUT": "1",
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "timeout leak", "--trusted-worker", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            receipt = json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["secret_leak_files"], [".env"])

    def test_rename_status_parser_keeps_destination_only(self) -> None:
        self.assertEqual(status_paths("R  new-name.txt\0old-name.txt\0"), ["new-name.txt"])

if __name__ == "__main__":
    unittest.main()
