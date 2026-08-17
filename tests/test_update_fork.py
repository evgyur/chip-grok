from __future__ import annotations

import json
import fcntl
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update-fork.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("chip_grok_update_fork", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


class UpdateForkTests(unittest.TestCase):
    def test_repository_urls_are_canonicalized_for_the_runtime_lock(self) -> None:
        updater = load_updater()
        expected = "https://github.com/evgyur/grok-build"
        self.assertEqual(updater.canonical_repository_url(expected + ".git"), expected)
        self.assertEqual(updater.canonical_repository_url("git@github.com:evgyur/grok-build.git"), expected)

    def fixture(self, root: Path, *, conflict: bool = False) -> tuple[Path, Path, Path]:
        upstream = root / "upstream"
        upstream.mkdir()
        git(upstream, "init", "-q", "-b", "main")
        git(upstream, "config", "user.name", "test")
        git(upstream, "config", "user.email", "test@example.test")
        (upstream / "shared.txt").write_text("base\n")
        (upstream / "upstream.txt").write_text("v1\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "base")

        fork_remote = root / "fork.git"
        run(["git", "clone", "-q", "--bare", str(upstream), str(fork_remote)])
        source = root / "source"
        run(["git", "clone", "-q", str(fork_remote), str(source)])
        git(source, "config", "user.name", "test")
        git(source, "config", "user.email", "test@example.test")
        git(source, "remote", "add", "upstream", str(upstream))
        if conflict:
            (source / "shared.txt").write_text("fork\n")
        else:
            (source / "patch.txt").write_text("chip patch\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "chip patch")
        git(source, "push", "-q", "origin", "main")
        return upstream, fork_remote, source

    def invoke(self, source: Path, state: Path) -> subprocess.CompletedProcess[str]:
        return run(
            [
                "python3",
                str(SCRIPT),
                "--fork-repo",
                str(source),
                "--state-root",
                str(state),
            ],
            check=False,
        )

    def test_no_change_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, source = self.fixture(root)
            result = self.invoke(source, root / "state")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_concurrent_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, source = self.fixture(root)
            state = root / "state"
            state.mkdir()
            with (state / "sync.lock").open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.invoke(source, state)
            self.assertEqual(result.returncode, 20)
            self.assertIn("already running", result.stderr)
            self.assertEqual(git(source, "status", "--short"), "")

    def test_clean_upstream_change_replays_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream, _, source = self.fixture(root)
            (upstream / "upstream.txt").write_text("v2\n")
            git(upstream, "add", "upstream.txt")
            git(upstream, "commit", "-qm", "upstream update")
            new_upstream = git(upstream, "rev-parse", "HEAD")

            result = self.invoke(source, root / "state")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CANDIDATE_READY", result.stdout)
            candidate = root / "state" / "candidates" / new_upstream[:12]
            report = json.loads((candidate / ".chip-sync-report.json").read_text())
            self.assertEqual(report["status"], "candidate_ready")
            self.assertEqual(report["upstream_head"], new_upstream)
            self.assertEqual(git(candidate, "merge-base", "HEAD", "upstream/main"), new_upstream)
            self.assertEqual((candidate / "patch.txt").read_text(), "chip patch\n")
            self.assertEqual((candidate / "upstream.txt").read_text(), "v2\n")

            resumed = self.invoke(source, root / "state")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("CANDIDATE_READY", resumed.stdout)
            self.assertEqual(git(candidate, "rev-parse", "HEAD"), report["candidate_head"])

    def test_conflict_preserves_candidate_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream, _, source = self.fixture(root, conflict=True)
            (upstream / "shared.txt").write_text("upstream\n")
            git(upstream, "add", "shared.txt")
            git(upstream, "commit", "-qm", "conflicting upstream update")
            new_upstream = git(upstream, "rev-parse", "HEAD")

            result = self.invoke(source, root / "state")
            self.assertEqual(result.returncode, 20)
            self.assertIn("shared.txt", result.stdout)
            candidate = root / "state" / "candidates" / new_upstream[:12]
            report = json.loads((candidate / ".chip-sync-report.json").read_text())
            self.assertEqual(report["status"], "conflict")
            self.assertEqual(report["conflicts"], ["shared.txt"])
            self.assertTrue(candidate.exists())

    def test_publish_failure_rolls_active_release_back(self) -> None:
        updater = load_updater()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate"
            candidate.mkdir()
            install_root = root / "install"
            old_release = install_root / "releases" / "old"
            new_release = install_root / "releases" / "new"
            old_release.mkdir(parents=True)
            new_release.mkdir(parents=True)
            current = install_root / "current"
            current.symlink_to(old_release)
            calls: list[tuple[str, ...]] = []

            def fake_git(_repo: Path, *args: str, **_kwargs: object) -> str:
                calls.append(tuple(args))
                if "--atomic" in args:
                    raise updater.SyncError("simulated atomic push failure")
                return ""

            def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if "install" in args:
                    current.unlink()
                    current.symlink_to(new_release)
                elif "rollback" in args:
                    current.unlink()
                    current.symlink_to(old_release)
                return subprocess.CompletedProcess(args, 0, "", "")

            report = {
                "candidate": str(candidate),
                "fork_head": "a" * 40,
                "candidate_head": "b" * 40,
                "upstream_head": "c" * 40,
                "lock": {"tag": "chip-v1.test"},
            }
            with mock.patch.object(updater, "git", side_effect=fake_git), mock.patch.object(
                updater, "run", side_effect=fake_run
            ):
                with self.assertRaises(updater.SyncError):
                    updater.publish_and_activate(
                        report, root / "grok", root / "fork.lock.json", root, install_root
                    )

            self.assertEqual(current.resolve(), old_release.resolve())
            self.assertTrue(any("--atomic" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
