from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "chip_grok.py"


class ChipGrokTests(unittest.TestCase):
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

    def test_prepare_creates_detached_clean_worktree(self) -> None:
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
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "write result", "--keep"],
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
                "Path('env.json').write_text(json.dumps(dict(os.environ)))\n"
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
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "inspect env", "--keep"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            receipt = json.loads(result.stdout)
            child = json.loads(Path(receipt["worktree"]).joinpath("env.json").read_text())
            self.assertEqual(child["SCOPED_TEST_KEY"], "scoped-value")
            self.assertNotIn("UNRELATED_GATEWAY_SECRET", child)

    def test_missing_worker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            env = os.environ | {
                "CHIP_GROK_BIN": str(root / "missing-grok"),
                "CHIP_GROK_WORKTREE_ROOT": str(root / "worktrees"),
            }
            result = subprocess.run(
                ["python3", str(SCRIPT), "run", "--repo", str(repo), "--task", "noop"],
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
            result = subprocess.run(
                ["python3", str(SCRIPT), "cleanup", "--repo", str(repo), "--worktree", str(repo)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing", json.loads(result.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
