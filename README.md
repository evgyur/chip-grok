# chip-grok

A public-safe Hermes skill for delegating coding tasks to **Grok Build** as a reviewed worker with explicit trust boundaries.

It keeps the useful part of Grok Build—repository tools, agent loop, and coding UI—while keeping merge and release authority in the supervising agent.

## What it does

- resolves an exact Git repository;
- creates a dedicated independent detached clone with an ownership receipt;
- runs Grok Build with a locally configured model alias;
- forbids commit, push, deploy, secrets, and production effects in the worker prompt;
- captures the result as JSON;
- leaves the diff for independent review and tests.

## Install for Hermes

```bash
git clone https://github.com/evgyur/chip-grok.git
cd chip-grok
./scripts/install.sh
```

Then reload skills in Hermes:

```text
/reload-skills
```

The canonical command is `/chip-grok`. The package also installs a small `/grok` compatibility skill that delegates to the same contract.

## Configure Grok Build

Install the verified downstream binary and lock together:

```bash
scripts/install-fork.sh install --binary /path/to/grok --lock /path/to/fork.lock.json
```

The runner opens and verifies the exact binary (hash plus embedded fork/upstream provenance) before creating a worker clone. This repository never stores provider credentials.

For a model already defined in `~/.grok/config.toml`:

```bash
export CHIP_GROK_MODEL=my-coding-model
export CHIP_GROK_PASSTHROUGH_ENV=MY_GATEWAY_API_KEY
```

To test an unactivated candidate, supply its executable and matching lock:

```bash
export CHIP_GROK_BIN=/path/to/candidate/grok
export CHIP_GROK_LOCK_FILE=/path/to/candidate/fork.lock.json
```

Wrappers and unverified binaries are rejected. See [`references/portable-setup.md`](references/portable-setup.md) and [`references/upstream-sync.md`](references/upstream-sync.md).

## Direct CLI use

```bash
python3 scripts/chip_grok.py run \
  --repo /path/to/repository \
  --task "Fix the parser bug and run focused tests" \
  --sandbox-profile strict
```

The command prints a machine-readable JSON receipt. It does **not** apply, commit, or push changes.

## Safety model

The runner fails closed unless either:

- `--sandbox-profile strict` requests Grok's enforced filesystem/process sandbox; or
- `--trusted-worker` explicitly acknowledges that Grok can access anything available to the current Unix user.

An independent clone protects the source Git database from normal worker Git operations, but it is not a host filesystem sandbox. Never describe a trusted-worker run as sandboxed.

Scoped provider values are removed from receipts and scanned with bounded streaming across tracked changes plus untracked/ignored outputs—including filenames and symlink targets—after normal exits and timeouts. An incomplete scan blocks completion. Cleanup requires the exact run token and refuses dirty or committed clones unless `--discard` is explicit.

Proxy variables are not inherited unless explicitly allowlisted. The source checkout must be clean and is fingerprinted before/after clone preparation and worker execution. The clone uses its own Git database without hardlinks or alternates, so worker commits, tags, and objects do not enter the source repository. Any worker commit is materialized back into a reviewable diff and blocks completion. Both normal exits and timeouts terminate the worker process group while timeout receipts retain worker HEAD evidence. Failed streaming diff/whitespace checks—including large untracked files—also block completion.

The supervising agent must inspect the diff and rerun tests before accepting changes.

## Development

```bash
./scripts/test.sh
```

## License

MIT
