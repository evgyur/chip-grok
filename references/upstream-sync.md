# Downstream fork maintenance

`evgyur/grok-build/main` is a generated linear downstream branch: one exact `xai-org/grok-build/main` commit followed only by the ordered Chip behavioral patch commits.

The local updater is the sole automatic maintenance entrypoint:

```bash
python3 scripts/update-fork.py \
  --fork-repo /path/to/grok-build \
  --verify \
  --h20-smoke
```

Default behavior is non-publishing. It fetches both remotes, detects the old upstream merge-base, rebases the linear patch stack onto the new upstream head, runs the bounded local gate, builds a release binary, and performs a real H20 smoke when requested.

Publication and activation are protected by both flags:

```bash
python3 scripts/update-fork.py \
  --fork-repo /path/to/grok-build \
  --verify \
  --h20-smoke \
  --publish-and-activate \
  --allow-main-rewrite
```

Before the first generated-branch rewrite, preserve the old remote `main` as an immutable rollback tag and obtain the explicit approval required by the canonical execution plan. The updater itself creates an archive tag and uses exact `--force-with-lease`; it never performs a blind force-push.

Outcomes:

- unchanged upstream: exit 0 with empty stdout;
- clean replay: preserve a candidate under `~/.local/state/chip-grok-sync/candidates/` and continue through the requested gate;
- conflict: exit 20, preserve the conflicted candidate and report exact paths;
- test/build/smoke failure: exit 20 without changing remote `main` or the active binary;
- successful approved activation: push the generated fork commit, tag it, and atomically switch the versioned local install.

GitHub Actions are not the runtime for this loop. The intended scheduler is one local Hermes job every 24 hours, installed only after the first manual execution proves the same updater path end to end.
