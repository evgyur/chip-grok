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

## Verified candidate override

To test a candidate before activation, point at the direct binary and its matching lock:

```bash
export CHIP_GROK_BIN=/path/to/candidate/grok
export CHIP_GROK_LOCK_FILE=/path/to/candidate/fork.lock.json
```

The runner rejects wrappers and unverified binaries. Provider credentials remain scoped environment variables named in `CHIP_GROK_PASSTHROUGH_ENV`.

The runner starts Grok with a minimal environment. Unrelated gateway/provider secrets are not inherited. Proxy variables are also excluded by default because proxy URLs may contain credentials. A provider or proxy variable is passed only when its name appears in `CHIP_GROK_PASSTHROUGH_ENV`; missing requested variables fail closed and allowed values are redacted from receipts.

## Sandbox note

Use Grok's `--sandbox strict`, not `workspace`, when you need enforced repository-scoped filesystem access. If strict mode fails with bubblewrap/user-namespace errors, do not claim sandboxing and do not silently continue. Either repair the host sandbox or make the full-trust decision explicit with `--trusted-worker`.

The independent clone protects the source Git database from normal worker Git operations, but it is not a host filesystem sandbox. A trusted same-user process can still read or write other user-accessible paths.
