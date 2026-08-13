---
name: chip-grok
description: "Use when delegating a coding task to Grok Build as a reviewed worker. Requires an enforced strict sandbox or explicit trusted-worker acknowledgement, uses an owned independent git clone, redacts scoped credentials, and requires independent verification."
argument-hint: "<coding task; optionally include repo=/absolute/path>"
version: 1.3.0
author: Evgeny "Chip" Yurchenko
license: MIT
metadata:
  hermes:
    tags: [coding, grok-build, worker, worktree, verification]
    related_skills: [shaw, codex]
---

# chip-grok

Delegate one coding task to Grok Build in an independent clone without confusing repository separation with process isolation.

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
2. Run `scripts/chip_grok.py prepare --repo <repo>` to create a dedicated independent detached clone under a configurable root and record its ownership token.
3. Write a bounded task prompt that includes:
   - goal and acceptance criteria;
   - inspect before editing;
   - smallest defensible diff;
   - preserve existing behavior and user changes;
   - run focused tests;
   - no commit, push, deployment, credential access, external publication, or destructive action.
4. Run with one explicit boundary:
   - `scripts/chip_grok.py run --repo <repo> --task <task> --sandbox-profile strict`; or
   - `scripts/chip_grok.py run --repo <repo> --task <task> --trusted-worker` only when the operator explicitly trusts Grok with every file available to the current Unix user.
5. Independently inspect the result:
   - `git status --short`;
   - `git diff --check`;
   - full diff and changed files;
   - focused tests selected from repository instructions and the task;
   - secret/private-marker scan over changed and untracked files.
6. Apply or copy changes back only after verification. The worker never owns merge, commit, push, release, or production effects.
7. Clean up with the exact `run_token`. Dirty cleanup requires explicit `--discard` after preserving or rejecting the diff.

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
- `CHIP_GROK_PASSTHROUGH_ENV` — comma-separated names of the scoped provider variables Grok needs. All unrelated parent-process secrets are removed from the worker environment.

Credentials stay in provider-specific environment variables or the local wrapper. Never place them in this repository, prompts, transcripts, or reports.

See [portable setup](references/portable-setup.md) for OpenAI-compatible model configuration and [Hermes adapter](references/hermes-adapter.md) for `/grok` alias installation.

## Safety boundaries

- One Grok writer per clone.
- A dedicated independent clone is mandatory for edits, but it is **not** a filesystem/process security boundary.
- Fail closed unless Grok's `strict` sandbox initializes successfully or `--trusted-worker` explicitly acknowledges full same-user host access.
- Deny network tools by default when the task does not need them.
- Never use auto-approval directly in a production checkout.
- No commits, pushes, PRs, deployments, migrations, secrets, payments, or production mutations by the worker.
- Pass only a scoped/revocable provider credential through `CHIP_GROK_PASSTHROUGH_ENV`; never expose the supervising gateway's full environment. Redact it from stdout/stderr and use a bounded streaming scan over tracked changes plus untracked/ignored outputs, filenames, and symlink targets after normal exits and timeouts. An incomplete scan blocks completion.
- Proxy variables are not inherited implicitly; if a provider genuinely requires one, name it explicitly in `CHIP_GROK_PASSTHROUGH_ENV` so its full value is redacted too.
- Cleanup requires a matching ownership receipt and run token. Refuse dirty or committed clones unless `--discard` is explicit.
- A worker-created or transient commit is a blocked result. The independent clone has its own Git database without hardlinks or alternates; commits are materialized back into a reviewable diff. Normal exits and timeouts terminate the worker process group; timeout receipts still report worker HEAD/commit state.
- Source repository must be clean before preparation; preparation and post-run checks fingerprint source state without writing Git objects.
- Failed diff/whitespace checks block completion, including streaming checks for large untracked files.
- Treat Grok output as a self-report until files and tests are verified independently.
- Public distributions must not include private endpoints, model-account names, chat IDs, hosts, local absolute paths, or credentials.

## Output contract

Return:

```text
status: completed | blocked
repo: <resolved repo>
base: <branch + HEAD>
clone: <dedicated path; receipt field remains `worktree` for compatibility>
run_token: <ownership token>
model_alias: <non-secret alias>
changed: <files>
verification: <commands and real results>
residual_risk: <none or exact risk>
next_effect: <apply/commit/push withheld or explicitly approved>
```

## Quick Test Checklist

- `python3 scripts/chip_grok.py prepare --repo <throwaway-repo>` returns an independent detached clone at the exact base HEAD.
- A cooperative fake worker creates a file inside the clone; an adversarial source-escape attempt is detected and blocks the receipt.
- Unsandboxed execution without `--trusted-worker` fails closed.
- Scoped credentials are redacted from output and detected in changed files.
- Implicit proxy credentials never reach the worker; worker commits and source HEAD/index mutations block; timeout descendants are killed.
- A missing Grok executable fails with `status: blocked`.
- Cleanup refuses paths outside `CHIP_GROK_WORKTREE_ROOT`.
- `python3 scripts/public_hygiene.py` reports `PUBLIC_HYGIENE_OK`.
- `./scripts/test.sh` passes before installation or publication.

## Done criteria

- Exact repository and base commit are recorded.
- Grok ran through the configured local provider route.
- Source checkout state did not change during the run; any detected source mutation blocks completion.
- Diff and focused tests were independently verified.
- No scoped credential entered the public receipt or changed files.
- No public/production effect occurred without separate approval.
