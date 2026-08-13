# Hermes adapter

A Hermes skill named `chip-grok` becomes `/chip-grok` after skill discovery reload.

## Install

```bash
./scripts/install.sh
```

The installer copies the complete package to:

```text
${HERMES_HOME:-$HOME/.hermes}/skills/chip-grok
```

The package includes a compatibility skill:

```text
/grok → load and follow /chip-grok
```

Then run `/reload-skills` in a live gateway session. No gateway config mutation or restart is required. A newly written skill file is not proof that the live slash-command cache has reloaded.

## Invocation contract

```text
/grok repo=/absolute/path Fix the failing parser and run its focused tests
```

The supervising Hermes agent follows `SKILL.md`, runs the bundled script by absolute path, reviews the diff, and reruns tests. `/grok` is not a persistent model switch and does not replace the active Hermes model.

## Why this is a skill, not a raw exec command

A raw quick-command subprocess does not reliably know which repository the conversation refers to and cannot supervise acceptance of a generated diff. Skill invocation keeps repository resolution, worktree preparation, verification, and approval boundaries inside the agent loop.
