# chip-grok

A public-safe Hermes skill for delegating coding tasks to **Grok Build** as a bounded worker.

It keeps the useful part of Grok Build—repository tools, agent loop, and coding UI—while keeping merge and release authority in the supervising agent.

## What it does

- resolves an exact Git repository;
- creates an isolated detached worktree;
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

The canonical command is `/chip-grok`. The installer can also add `/grok` as a quick alias when `hermes` is available.

## Configure Grok Build

Install and configure Grok Build separately. This repository never stores provider credentials.

For a model already defined in `~/.grok/config.toml`:

```bash
export CHIP_GROK_MODEL=my-coding-model
```

Or point at a trusted local wrapper that already selects the provider/model:

```bash
export CHIP_GROK_BIN="$HOME/.local/bin/grok-my-provider"
```

See [`references/portable-setup.md`](references/portable-setup.md).

## Direct CLI use

```bash
python3 scripts/chip_grok.py run \
  --repo /path/to/repository \
  --task "Fix the parser bug and run focused tests"
```

The command prints a machine-readable JSON receipt. It does **not** apply, commit, or push changes.

## Safety model

The isolated worktree is the primary boundary. Grok's optional OS sandbox is defense-in-depth because some server environments disable user namespaces.

The supervising agent must inspect the diff and rerun tests before accepting changes.

## Development

```bash
./scripts/test.sh
```

## License

MIT
