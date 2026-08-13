#!/usr/bin/env python3
"""Fail closed on secret-shaped or private-environment material."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {"", ".md", ".py", ".sh", ".yml", ".yaml", ".toml", ".txt"}

# Build examples dynamically so the scanner does not contain its own sentinel.
RULES = {
    "private-home": re.compile(r"/(?:home|Users)/(?!runner(?:/|$)|path(?:/|$)|repository(?:/|$))[A-Za-z0-9._-]+/"),
    "private-service-path": re.compile(r"/(?:opt|srv)/[A-Za-z0-9._-]+"),
    "telegram-chat-id": re.compile(r"-100\d{7,}"),
    "private-ip": re.compile(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}\.\d{1,3}\b"),
    "token-assignment": re.compile(r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][^$<{.][^'\"]{7,}"),
    "private-key": re.compile("BEGIN " + "PRIVATE KEY"),
    "github-token": re.compile("gh" + "[opusr]_[A-Za-z0-9]{20,}"),
    "openai-token": re.compile("sk" + "-[A-Za-z0-9]{20,}"),
}


def files() -> list[Path]:
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        )
        return [ROOT / line for line in result.stdout.splitlines() if line]
    return [p for p in ROOT.rglob("*") if p.is_file()]


def main() -> int:
    findings: list[str] = []
    for path in files():
        if path.suffix not in TEXT_SUFFIXES or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, rule in RULES.items():
                if rule.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{number}:{name}")
    if findings:
        print("\n".join(findings))
        return 1
    print("PUBLIC_HYGIENE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
