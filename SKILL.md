---
name: chip-grok
description: "Use when delegating a coding task to Grok Build as a bounded worker. Runs Grok in an isolated git worktree, keeps provider credentials outside the skill, and requires independent diff/test verification before applying changes."
argument-hint: "<coding task; optionally include repo=/absolute/path>"
version: 1.0.0
author: Evgeny "Chip" Yurchenko
license: MIT
metadata:
  hermes:
    tags: [coding, grok-build, worker, worktree, verification]
    related_skills: [shaw, codex]
---

# chip-grok

Delegate one coding task to Grok Build without turning it into an unmanaged writer.

## Trigger

Use for `/chip-grok`, `/grok`, or explicit requests to code through Grok Build. Do not use it for general questions about Grok or xAI.

## Inputs

- A concrete coding task with acceptance criteria.
- Repository path. Prefer `repo=/absolute/path` in the request. Otherwise resolve it from current project context and verify with `git rev-parse --show-toplevel`.
- A local Grok provider/model configuration. The public skill contains no API keys, private endpoints, or account-specific model aliases.

If the task is missing, return:

```text
Использование: /grok repo=/path/to/repo <задача и критерии готовности>
```

## Workflow

1. Inspect the source repository before delegating:
   - resolve the exact root;
   - record branch, HEAD, and `git status --short`;
   - stop if the requested task is ambiguous or the repository is not identifiable;
   - never overwrite or absorb unrelated dirty changes.
2. Run `scripts/chip_grok.py prepare --repo <repo>` to create a detached isolated worktree under a configurable worktree root.
3. Write a bounded task prompt that includes:
   - goal and acceptance criteria;
   - inspect before editing;
   - smallest defensible diff;
   - preserve existing behavior and user changes;
   - run focused tests;
   - no commit, push, deployment, credential access, external publication, or destructive action.
4. Run `scripts/chip_grok.py run --repo <repo> --task <task>` or invoke the configured Grok wrapper in the prepared worktree.
5. Independently inspect the result:
   - `git status --short`;
   - `git diff --check`;
   - full diff and changed files;
   - focused tests selected from repository instructions and the task;
   - secret/private-marker scan over changed and untracked files.
6. Apply or copy changes back only after verification. The worker never owns merge, commit, push, release, or production effects.
7. Clean up the temporary worktree after preserving any needed patch/evidence.

## Runtime configuration

Default command:

```bash
grok -m "$CHIP_GROK_MODEL"
```

Supported environment variables:

- `CHIP_GROK_BIN` — Grok executable or a trusted local wrapper; default `grok`.
- `CHIP_GROK_MODEL` — configured Grok model alias; required unless the wrapper selects one.
- `CHIP_GROK_WORKTREE_ROOT` — worktree parent; default `$TMPDIR/chip-grok-worktrees`.
- `CHIP_GROK_MAX_TURNS` — bounded agent turns; default `60`.
- `CHIP_GROK_TIMEOUT` — process timeout in seconds; default `1800`.

Credentials stay in provider-specific environment variables or the local wrapper. Never place them in this repository, prompts, transcripts, or reports.

See [portable setup](references/portable-setup.md) for OpenAI-compatible model configuration and [Hermes adapter](references/hermes-adapter.md) for `/grok` alias installation.

## Safety boundaries

- One Grok writer per worktree.
- Worktree isolation is mandatory for edits.
- Grok's own sandbox is defense-in-depth only; do not claim isolation unless the host proves it works.
- Deny network tools by default when the task does not need them.
- Never use auto-approval directly in a production checkout.
- No commits, pushes, PRs, deployments, migrations, secrets, payments, or production mutations by the worker.
- Treat Grok output as a self-report until files and tests are verified independently.
- Public distributions must not include private endpoints, model-account names, chat IDs, hosts, local absolute paths, or credentials.

## Output contract

Return:

```text
status: completed | blocked
repo: <resolved repo>
base: <branch + HEAD>
worktree: <isolated path>
model_alias: <non-secret alias>
changed: <files>
verification: <commands and real results>
residual_risk: <none or exact risk>
next_effect: <apply/commit/push withheld or explicitly approved>
```

## Quick Test Checklist

- `python3 scripts/chip_grok.py prepare --repo <throwaway-repo>` returns a detached worktree at the exact base HEAD.
- A fake worker can create a file only inside that worktree; the source checkout stays unchanged.
- A missing Grok executable fails with `status: blocked`.
- Cleanup refuses paths outside `CHIP_GROK_WORKTREE_ROOT`.
- `python3 scripts/public_hygiene.py` reports `PUBLIC_HYGIENE_OK`.
- `./scripts/test.sh` passes before installation or publication.

## Done criteria

- Exact repository and base commit are recorded.
- Grok ran through the configured local provider route.
- Edits happened only in the isolated worktree.
- Diff and focused tests were independently verified.
- No credential or private runtime material entered the worktree or report.
- No public/production effect occurred without separate approval.
