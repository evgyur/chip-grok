from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        required = [
            "SKILL.md",
            "README.md",
            "LICENSE",
            "scripts/chip_grok.py",
            "scripts/install.sh",
            "scripts/test.sh",
            "scripts/public_hygiene.py",
            "aliases/grok/SKILL.md",
            "references/portable-setup.md",
            "references/hermes-adapter.md",
        ]
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_skill_frontmatter_and_command_contract(self) -> None:
        text = (ROOT / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: chip-grok", text)
        self.assertIn("/grok", text)
        self.assertIn("dedicated worktree", text)
        self.assertIn("not** a filesystem/process security boundary", text)
        self.assertNotIn("api_key = \"sk-", text)

        alias = (ROOT / "aliases" / "grok" / "SKILL.md").read_text()
        self.assertIn("name: grok", alias)
        self.assertIn("skill_view(name='chip-grok')", alias)

    def test_installer_uses_full_directory_package(self) -> None:
        text = (ROOT / "scripts/install.sh").read_text()
        for marker in [".gitignore", ".github", "aliases", "references", "scripts", "tests"]:
            self.assertIn(marker, text)
        self.assertIn("skill-backups", text)
        self.assertNotIn('BACKUP="$TARGET.backup.', text)

    def test_shell_syntax(self) -> None:
        scripts = sorted((ROOT / "scripts").glob("*.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_gitignore_protects_runtime_and_secrets(self) -> None:
        text = (ROOT / ".gitignore").read_text()
        for marker in [".env", ".grok/", ".shaw/", ".supergoal/", "receipts/"]:
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
