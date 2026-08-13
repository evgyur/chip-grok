---
name: grok
description: "Use when /grok should delegate a coding task through the installed chip-grok worker contract. Compatibility alias; load chip-grok before acting."
argument-hint: "repo=/absolute/path <coding task>"
version: 1.0.0
license: MIT
---

# grok compatibility command

Load `chip-grok` with `skill_view(name='chip-grok')`, then follow that skill exactly for the user's instruction. See [canonical workflow](references/canonical-workflow.md).

Do not reinterpret `/grok` as an xAI model switch. It invokes Grok Build as a bounded coding worker through the locally configured provider route.

## Output Contract

Use the `chip-grok` output contract unchanged.

## Quick Test Checklist

- `scan_skill_commands()` resolves `/grok` to this compatibility skill.
- `skill_view(name='chip-grok')` succeeds before execution.

## Done Criteria

- The canonical `chip-grok` workflow was loaded and followed.