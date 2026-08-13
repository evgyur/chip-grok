# Portable Grok Build setup

`chip-grok` expects Grok Build to be installed and a model to be configured locally. It does not install Grok, create provider accounts, or copy credentials.

## OpenAI-compatible provider

Grok Build supports custom models in `~/.grok/config.toml`:

```toml
[model.my-coding-model]
model = "provider-model-id"
base_url = "https://gateway.example/v1"
name = "My coding model"
env_key = "MY_GATEWAY_API_KEY"
api_backend = "chat_completions"
auth_scheme = "bearer"
context_window = 128000
max_completion_tokens = 32768
max_turns = 60
```

Keep the bearer value in the named environment variable:

```bash
export MY_GATEWAY_API_KEY="..."
export CHIP_GROK_MODEL="my-coding-model"
export CHIP_GROK_PASSTHROUGH_ENV="MY_GATEWAY_API_KEY"
```

Run a harmless model smoke before coding:

```bash
grok -m "$CHIP_GROK_MODEL" \
  -p "Reply exactly: READY" \
  --output-format json \
  --no-subagents \
  --disable-web-search \
  --tools ""
```

For normal chip-grok execution, prefer:

```bash
python3 scripts/chip_grok.py run \
  --repo /path/to/repo \
  --task "Implement the change and run focused tests" \
  --sandbox-profile strict
```

If the host cannot initialize Grok's strict sandbox, the runner fails closed. `--trusted-worker` is an explicit acknowledgement that the Grok process has the same filesystem visibility as the current Unix user; use it only for a fully trusted worker on a controlled host.

## Trusted wrapper

If a local wrapper selects the model and loads provider environment safely:

```bash
export CHIP_GROK_BIN="$HOME/.local/bin/grok-my-provider"
```

The wrapper path is local operator configuration. Do not commit it to a public repository. If the wrapper needs a scoped credential from its parent process, name only that variable in `CHIP_GROK_PASSTHROUGH_ENV`.

The runner starts Grok with a minimal environment. Unrelated gateway/provider secrets are not inherited. A provider variable is passed only when its name appears in `CHIP_GROK_PASSTHROUGH_ENV`; missing requested variables fail closed.

## Sandbox note

Use Grok's `--sandbox strict`, not `workspace`, when you need enforced repository-scoped filesystem access. If strict mode fails with bubblewrap/user-namespace errors, do not claim sandboxing and do not silently continue. Either repair the host sandbox or make the full-trust decision explicit with `--trusted-worker`.

The worktree is only a change-management boundary. A trusted same-user process can follow Git metadata back to the source checkout or read other user files.
