# План: собственный fork Grok Build + тонкий `/grok`-адаптер

**Статус:** proposal, без реализации
**Дата проверки:** 2026-08-13
**Текущий skill:** `evgyur/chip-grok` at `f323c01c268061446f79516f4fa11d1eed9f507a` (`v1.3.1`)
**Upstream:** `xai-org/grok-build` public `main` at `e5fd4816d43260c15ba785f103990c1ed6cea230`
**Upstream monorepo source:** `SOURCE_REV=ea094a8c369475f97c85540d01730baec0dce5d6`
**Observed official binary:** `grok 1.0.3 (1a29d5bc12)`
**Upstream package version:** `xai-grok-pager-bin 1.0.3`

## 1. Решение

Создать публичный Apache-2.0 fork `evgyur/grok-build`, держать в нём маленький проверяемый patch stack для агентного worker-режима и собирать собственный pinned binary. `chip-grok` оставить отдельным MIT-репозиторием и Hermes-адаптером.

Архитектура:

```text
xai-org/grok-build (upstream source)
          │ fetch/rebase + diff report
          ▼
evgyur/grok-build (our fork, Apache-2.0)
  ├─ minimal worker-contract patches
  ├─ provenance/version endpoint
  ├─ CI + signed/checksummed binaries
  └─ upstream sync PRs
          │ exact release manifest + sha256
          ▼
evgyur/chip-grok (Hermes skill, MIT)
  ├─ selects exact fork binary
  ├─ creates independent clone
  ├─ scopes env/provider credentials
  ├─ launches review/implement/full modes
  ├─ exports declared artifacts
  └─ verifies receipt/diff/tests before adoption
```

**Не делать:** не переносить весь orchestration в Python skill, не переписывать Grok runtime, не vendor'ить upstream source внутрь `chip-grok`, не заменять официальный `grok` в `$PATH` незаметно, не auto-merge'ить upstream.

**Patch budget:** максимум 6 fork-only commits и ориентир не более 1% first-party source lines. Если три последовательных upstream sync требуют конфликтов в одном и том же patch или upstream выпускает эквивалентный native contract, сначала уменьшаем/удаляем patch, а не наращиваем fork.

## 2. Почему сейчас нельзя честно «поручить ему вообще всё»

Grok Build уже умеет много: headless JSON, JSON Schema, custom models, subagents и concurrency limits, hooks, plugins, MCP, background tasks, sessions, memory, tool allow/deny и sandbox profiles. Проблема не в количестве функций, а в отсутствии стабильного оркестраторского ABI и наших гарантий вокруг него.

Сейчас `chip-grok` вынужден снаружи:

1. создавать независимый clone;
2. ограничивать environment и передавать только named provider key;
3. выключать memory/web/subagents;
4. парсить generic headless JSON;
5. определять commit/tag/diff/source mutation постфактум;
6. вручную переносить requested artifacts из disposable clone;
7. отличать полезный результат от `.venv`, cache и test residue;
8. убивать всю process group по timeout/normal exit;
9. не давать official auto-update незаметно заменить tested binary.

Из-за этого Grok может выполнить работу, но supervising agent всё ещё вручную склеивает результат. Fork нужен, чтобы сделать эти границы **первоклассным worker contract**, а не ещё большим prompt'ом.

### Что можно делегировать целиком после переделки

- чтение репозитория и инструкций;
- discovery/review с внутренними subagents;
- создание плана;
- RED-тесты и реализацию;
- локальные lint/type/test/build команды;
- повторную самопроверку;
- подготовку declared artifacts и machine-readable evidence;
- полный цикл `review → fix → verify` внутри одного disposable workspace.

### Что остаётся у Hermes по принципу authority boundary

- выбор source repo, branch и exact base SHA;
- передача provider credential;
- решение, какой artifact/diff принять;
- commit/push/PR/merge/release/deploy;
- production, деньги, secrets и внешние side effects.

Это не недостаток Grok. Эти полномочия нельзя отдавать same-user coding process без отдельного approval/capability boundary.

## 3. Что подтвердилось по upstream

- Source доступен: `https://github.com/xai-org/grok-build`.
- Лицензия first-party кода: Apache License 2.0; third-party notices обязательны.
- Public repo периодически синхронизируется из внутреннего monorepo; `SOURCE_REV` хранит настоящий monorepo commit.
- На момент проверки public repo не публикует Git tags/GitHub Releases и не содержит GitHub Actions workflows.
- Public `main` commit unsigned и branch protection через API не обнаружен.
- Upstream активно меняется: 7 public sync commits за 7 дней, 28 за 30 дней.
- Build требует Rust `1.94.0`, rustfmt/clippy, DotSlash и protoc path; на текущем host Rust/DotSlash не установлены.
- Docker доступен и пригоден для reproducible build/sandbox test lane.
- Official binary stable channel сейчас `1.0.3`; auto-update включён.
- Local H20 route использует loopback OpenAI-compatible endpoint, model alias `h20-gpt`, bearer env key; credential нельзя помещать в fork/config/release artifacts.

## 4. Репозитории, branches и remotes

### 4.1 Новый fork: `evgyur/grok-build`

Лицензия остаётся Apache-2.0. Сохранить `LICENSE`, `THIRD-PARTY-NOTICES`, crate notices и vendored notices. Добавить `NOTICE-CHIP.md` с явным перечнем изменённых файлов и назначением fork patches.

Локальные remotes:

```bash
git remote rename origin upstream
git remote add origin git@github.com:evgyur/grok-build.git
git remote -v
```

Branches:

- `main` — последний одобренный upstream SHA + наш линейный patch stack;
- `sync/upstream-YYYYMMDD-<sha>` — update candidate PR;
- `release/chip-<version>` — frozen release candidate только при необходимости remediation;
- никаких permanent long-lived feature branches для upstream sync.

Tags/releases:

```text
chip-v1.0.3.1
chip-v1.0.3.2
...
```

Каждый release manifest хранит:

- upstream public commit;
- upstream `SOURCE_REV`;
- fork commit/tree;
- package/base version;
- Rust toolchain;
- `Cargo.lock` SHA-256;
- binary SHA-256;
- container image digest;
- enabled Cargo features;
- test receipts;
- license/notices digest.

### 4.2 Existing adapter: `evgyur/chip-grok`

Остаётся отдельным репозиторием. Он не копирует Rust source и не следит за upstream source напрямую во время обычного `/grok` run. Он потребляет **один exact fork release manifest**.

Добавить:

```text
references/fork-contract.md
references/upstream-sync.md
schemas/worker-request-v1.json
schemas/worker-receipt-v1.json
scripts/install-fork.sh
scripts/verify-fork.py
scripts/export-artifacts.py
fork.lock.json
```

`fork.lock.json` — единственная runtime SSOT:

```json
{
  "schema": 1,
  "repository": "https://github.com/evgyur/grok-build",
  "tag": "chip-v1.0.3.1",
  "fork_commit": "<sha>",
  "upstream_commit": "e5fd4816d43260c15ba785f103990c1ed6cea230",
  "upstream_source_rev": "ea094a8c369475f97c85540d01730baec0dce5d6",
  "platform": "linux-x86_64",
  "sha256": "<binary-sha256>"
}
```

## 5. Минимальный patch stack в fork

Цель: максимум 4–6 маленьких commits, каждый с отдельными tests. Любая функция, уже имеющаяся upstream, используется как есть.

### Patch 1 — fork provenance и managed-update boundary

**Файлы upstream:**

- `crates/codegen/xai-grok-pager-bin/build.rs`
- `crates/codegen/xai-grok-pager-bin/src/main.rs`
- `crates/codegen/xai-grok-pager/src/app/cli.rs`
- новый маленький provenance module/test рядом с version command

Добавить machine-readable:

```bash
grok version --json
```

```json
{
  "version": "1.0.3",
  "distribution": "chip",
  "fork_commit": "...",
  "upstream_commit": "...",
  "upstream_source_rev": "...",
  "worker_contracts": [1],
  "auto_update": "externally-managed"
}
```

Для `distribution=chip`:

- background official auto-update выключен compile-time/runtime;
- `grok update` не скачивает x.ai binary, а возвращает `EXTERNALLY_MANAGED` и URL инструкции;
- fork binary никогда не заменяет себя official binary автоматически.

**RED tests:** version JSON содержит provenance; `update --check --json` не пишет на disk; ordinary startup не запускает official updater.

### Patch 2 — stable worker contract v1

Не создавать отдельный второй runtime. Добавить к existing headless path один флаг:

```bash
grok --worker-contract 1 --worker-request /path/request.json
```

Worker request:

```json
{
  "contract_version": 1,
  "run_id": "uuid",
  "cwd": "/workspace",
  "task": "...",
  "model": "h20-gpt",
  "reasoning_effort": "xhigh",
  "max_turns": 60,
  "subagents": {"enabled": true, "max_depth": 1, "max_concurrent": 3},
  "web": false,
  "memory": false,
  "ask_user": false,
  "background_wait_seconds": 600,
  "sandbox": {"profile": "strict", "required": true},
  "deliverables": [".hermes/plans/review.md"],
  "tools": ["read_file", "grep", "list_dir", "search_replace", "run_terminal_cmd"]
}
```

Stable receipt written atomically to requested output path and stdout:

```json
{
  "contract_version": 1,
  "run_id": "uuid",
  "status": "completed|blocked|timeout|cancelled|infra_error",
  "stop_reason": "end_turn|max_turns|sandbox_unavailable|...",
  "session_id": "...",
  "model": "h20-gpt",
  "turns": 12,
  "usage": {},
  "sandbox": {"requested": "strict", "enforced": true, "platform": "linux"},
  "subagents": {"started": 3, "completed": 3, "failed": 0},
  "background": {"remaining": 0, "timed_out": false},
  "deliverables": [{"path": ".hermes/plans/review.md", "sha256": "...", "bytes": 1234}],
  "commands": [{"command_label": "pytest", "exit_code": 0}],
  "summary": "...",
  "errors": []
}
```

Rules:

- receipt schema is independent from generic ACP/headless JSON;
- invalid request fails before model/network/process side effects;
- receipt is written for spawn/config/sandbox/model/max-turn/timeout/cancel paths;
- bounded strings and arrays; no raw env, prompts, secrets or full terminal dumps;
- deliverable paths must be relative, normalized, non-symlink escapes, regular files, bounded size;
- no commit/push/deploy policy stays in orchestrator/prompt until a real capability gate exists; do not claim prompt text is enforcement.

### Patch 3 — fail-closed sandbox evidence

Использовать existing `xai-grok-sandbox` support/enforcement data, не писать новый sandbox.

Добавить:

- `sandbox.required=true` → non-zero + receipt `sandbox_unavailable`, если enforcement не активировался;
- receipt сообщает requested/effective profile, platform, `enforced`, network restriction;
- test lane с `SANDBOX_E2E_REQUIRE_ENFORCEMENT=1`;
- no fallback to trusted mode.

Для текущего server, где native bwrap user namespaces не работают, production worker lane запускается внутри Docker с:

- read-only base image;
- one writable workspace mount;
- ephemeral `$HOME`, `$GROK_HOME`, XDG dirs and `/tmp`;
- no Docker socket;
- network allow only to local H20 proxy path when required;
- dropped capabilities, `no-new-privileges`, pids/memory/cpu limits;
- exact image digest.

Trusted-host mode остаётся explicit development fallback, не default.

### Patch 4 — artifact and residue semantics

Worker contract различает:

- `deliverables` — файлы, которые заказаны и экспортируются;
- `workspace_changes` — source diff/untracked files;
- `runtime_residue` — `.venv`, caches, build directories, test output;
- `external_effects` — всегда blocked unless separately approved.

Grok не должен самостоятельно копировать artifacts в source checkout. Он только выдаёт checksummed manifest. `chip-grok` экспортирует allowlisted deliverables в staging, проверяет hash/size/type, затем атомарно переносит их по явному destination mapping.

Это закрывает текущую ручную операцию «найти `.md` в clone и перенести» и позволяет поручить review+plan целиком.

### Patch 5 — worker contract regression suite

Новые tests должны использовать mock provider, а не H20 secret:

- valid review request + one `.md` deliverable;
- internal subagents obey max depth/concurrency;
- max turns returns receipt;
- model/API failure returns receipt;
- non-executable/missing tool path returns receipt;
- strict sandbox unavailable fails closed;
- symlink/path traversal artifact rejected;
- oversized artifact rejected;
- background descendant cancelled;
- memory/web/plugins/hooks/MCP absent unless request enables exact capability;
- receipt never contains seeded secret/canary;
- official auto-update cannot replace fork binary;
- SIGTERM/SIGINT produce bounded terminal receipt when feasible.

## 6. Переделка `chip-grok`

### 6.1 Runtime selection

Default:

```text
CHIP_GROK_BIN=~/.local/lib/chip-grok/bin/grok
```

Runner до clone/model call выполняет:

1. binary exists and executable;
2. `grok version --json` parses;
3. `distribution == chip`;
4. fork/upstream/source revisions match `fork.lock.json`;
5. binary SHA-256 matches;
6. worker contract v1 is advertised;
7. official binary fallback запрещён по умолчанию.

Explicit diagnostic override `--official-worker` может использовать official binary только для comparison smoke; он не получает release authority и не подменяет default.

### 6.2 Ephemeral runtime home

На каждый run adapter создаёт private runtime tree:

```text
run-root/
  grok-home/config.toml
  home/
  xdg-config/
  xdg-cache/
  tmp/
  request.json
  receipt.json
  artifacts-stage/
  workspace/
```

В ephemeral `config.toml` копируется только выбранная model definition без credential value. Provider key передаётся named env allowlist. Real `$HOME` и real `~/.grok` worker не видит.

### 6.3 Modes

Сделать modes явными, не выводить их из свободного prompt:

```text
/grok review repo=... output=.hermes/plans/review.md
/grok implement repo=... task="..."
/grok full repo=... task="review, fix, verify"
/grok compare-upstream repo=...   # diagnostic only
```

- `review`: read-only source changes; один или несколько declared `.md/.json` artifacts;
- `implement`: diff разрешён, commits/tags блокируют adoption;
- `full`: Grok внутри одного clone делает discovery → RED → fix → tests → self-review; Hermes принимает только final receipt/diff;
- `compare-upstream`: сравнивает fork binary с official smoke, ничего не меняет.

Subagents default для `review/full`: enabled, max depth `1`, max concurrent `3`. Для маленьких implementation tasks: disabled unless requested. Лимиты фиксируются в request/receipt.

### 6.4 Acceptance

Hermes больше не пересказывает весь worker процесс. Он делает bounded acceptance:

1. validate receipt schema/correlation/run ID;
2. verify fork provenance and sandbox receipt;
3. verify source fingerprint and independent clone ownership;
4. re-hash declared artifacts;
5. inspect diff/plan;
6. rerun focused tests independently where code changed;
7. export/adopt artifact;
8. cleanup only after adoption receipt.

## 7. Upstream update loop

### 7.1 Monitor

GitHub Actions в fork каждые 6 часов и manual dispatch:

1. fetch `https://github.com/xai-org/grok-build.git main`;
2. compare public commit + `SOURCE_REV` to last accepted manifest;
3. if unchanged: silent success;
4. if changed: create/update one `sync/upstream-<shortsha>` PR;
5. never auto-merge.

Monitor output must be stable and include:

- old/new public SHA and `SOURCE_REV`;
- commit/tree range;
- changed crates/files;
- CLI help diff;
- config schema diff;
- worker-relevant docs diff: headless, subagents, hooks, MCP, plugins, sandbox, background tasks;
- `Cargo.lock`, toolchain, license/notices changes;
- patch conflicts;
- upstream changelog entries where release/source mapping is provable.

Because upstream public commits are unsigned and have no tags/releases, **SHA + tree + SOURCE_REV + fetched timestamp** are required. Changelog version alone is not source identity.

### 7.2 Rebase policy

For each sync PR:

```bash
git fetch upstream main
git switch -c sync/upstream-YYYYMMDD-<sha> upstream/main
git cherry-pick <ordered chip patch commits>
```

Prefer replaying a tiny patch stack to merging upstream into a heavily divergent branch. Every conflict is resolved in the patch that owns the behavior. Generated root `Cargo.toml` is treated read-only as upstream requests.

### 7.3 Feature harvest

The sync PR bot classifies upstream deltas:

- **adopt automatically through rebase:** bug fixes/internal improvements that preserve worker ABI;
- **expose in `chip-grok`:** new stable headless/subagent/artifact/sandbox capabilities useful to Hermes;
- **keep disabled:** interactive UI, remote workspace exposure, sharing, telemetry/upload, auto-update, global memory unless explicitly needed;
- **manual security review:** permissions, terminal execution, hooks/plugins/MCP, workspace server, auth, update, sandbox, network, persistence.

Each useful upstream feature becomes a small adapter issue/PR. The sync itself must not silently enable it.

## 8. CI and release gates

### Fork PR CI — no secrets

1. license/notices integrity;
2. `cargo fmt --all -- --check`;
3. targeted `cargo check`/`clippy` for changed crates;
4. worker contract unit/integration tests;
5. mock-provider headless E2E;
6. Docker enforced-sandbox E2E;
7. secret/canary scan;
8. updater-disabled regression;
9. build Linux x86_64 artifact;
10. generate SBOM, checksums, provenance manifest.

Do not require full workspace on every PR if it is prohibitively slow; compute changed-crate closure. Run full broader matrix nightly/release.

### Release candidate gate

- exact fork SHA frozen;
- clean source clone;
- Cargo.lock/toolchain pinned;
- release build in pinned container image;
- mock E2E green;
- local H20 `h20-gpt` smoke using environment secret and loopback route;
- one real `review` artifact smoke;
- one real `implement` diff smoke;
- process-group/timeout smoke;
- fresh install of `chip-grok` against candidate binary;
- independent read-only diff/provenance review;
- signed/checksummed GitHub Release and immutable manifest;
- then update `chip-grok/fork.lock.json` in a separate PR.

No automatic production activation from fork release. Adapter lock promotion is the approval boundary.

## 9. Implementation sequence

### Phase 0 — fork and baseline, no behavioral patches

1. Create public GitHub fork `evgyur/grok-build`.
2. Add `upstream` remote and verify ancestry/tree.
3. Add `NOTICE-CHIP.md`, fork README section, security/update policy.
4. Add CI skeleton and pinned build container.
5. Build unmodified source; prove resulting binary runs `version --json`, headless mock smoke and current H20 custom-model smoke.
6. Record baseline manifest.

**Done:** reproducible unmodified fork build exists; no claim that it is default worker yet.

### Phase 1 — provenance + updater boundary

1. RED tests for provenance and no official self-update.
2. Implement Patch 1.
3. Build/test/review exact diff.
4. Publish first non-default prerelease.

**Done:** binary identity cannot be confused with official binary.

### Phase 2 — worker contract v1

1. Add JSON schemas and RED tests.
2. Implement request validation and stable receipt around existing headless runtime.
3. Add sandbox evidence/fail-closed path.
4. Add deliverables manifest.
5. Run mock E2E and adversarial tests.

**Done:** one command returns a complete correlated receipt on all tested terminal paths.

### Phase 3 — adapter migration

1. Add `fork.lock.json` and `verify-fork.py` to `chip-grok`.
2. Add isolated per-run HOME/GROK_HOME/XDG/TMP.
3. Replace generic headless prompt launch with worker request v1.
4. Add `review`, `implement`, `full` modes.
5. Add atomic artifact export/adoption receipt.
6. Keep existing clone/source/secret/process tests until fork contract proves equivalent coverage.
7. Install candidate only in temporary Hermes home; verify `/grok` alias.

**Done:** Continuum review reproduces end-to-end with plan automatically delivered, no manual clone spelunking.

### Phase 4 — upstream sync automation

1. Add scheduled monitor and sync PR workflow.
2. Add deterministic upstream diff report.
3. Add patch replay script.
4. Add feature-harvest labels/checklist.
5. Dry-run against at least two historical upstream sync commits.

**Done:** new upstream commit creates one reviewable PR; no duplicate PRs, no auto-merge, no binary activation.

### Phase 5 — promote default

1. Freeze exact fork release.
2. Run release candidate gate.
3. Update `chip-grok/fork.lock.json`.
4. CI/fresh clone/install/self-update tests.
5. Install active skill and fork binary.
6. Keep official binary at separate path for rollback/comparison.
7. Verify `/grok review`, `/grok implement`, `/grok full` live.

**Done:** `/grok` uses verified fork by default; rollback is one lock/install operation.

## 10. Test scenarios that must pass before default switch

- Upstream source sync with no conflicts.
- Upstream sync with conflict in CLI parser.
- Upstream adds a useful headless flag; report detects it, adapter remains unchanged until explicit integration.
- Upstream changes license/notices; release blocks pending review.
- Fork binary tries official auto-update; test blocks.
- Wrong binary/fork SHA/version; adapter blocks before clone/model call.
- Missing H20 env key; no workspace is created.
- Credential printed by model/tool; receipt and artifacts block/redact.
- Real HOME canary cannot be read or written in sandbox lane.
- Strict sandbox unavailable; no trusted fallback.
- Review creates plan plus `.venv`; only declared plan is exported, residue is discarded.
- Worker spawns three subagents; fourth is queued/rejected per request.
- Worker max-turns/timeout/model error returns valid receipt.
- Worker commits/tags; adapter preserves clone and blocks adoption.
- Worker changes source checkout by deliberate host escape in trusted mode; fingerprint blocks and reports damage.
- Normal exit leaves background descendant; process lifecycle test fails.
- Artifact uses symlink/absolute/`..` escape; export blocks.
- Fresh clone of both repos can install and run with no hidden local files.
- Rollback to previous fork release restores working `/grok` without touching provider config.

## 11. Rollback

Keep:

```text
~/.local/lib/chip-grok/releases/<tag>/grok
~/.local/lib/chip-grok/current -> releases/<approved-tag>
~/.local/bin/grok             # official, untouched
```

`chip-grok` invokes `current/grok`, not ambient `grok`. Rollback updates `current` atomically to previous verified tag after checksum/provenance verification. Skill rollback restores previous `fork.lock.json` and package backup. Provider config/key do not change.

## 12. Risks and controls

| Risk | Control |
|---|---|
| Upstream churn makes fork expensive | Keep patch stack under 4–6 focused commits; no broad refactor |
| Public source does not map cleanly to official binary | Track public SHA + `SOURCE_REV` + package version; do not claim byte equivalence |
| Unsigned/no-tag upstream commits | Pin exact SHA/tree, fetch over GitHub, preserve manifests; manual sync approval |
| Apache/third-party compliance drift | CI hashes/checks notices and blocks removed attribution |
| Official updater overwrites fork | Externally-managed update mode + regression test + separate install path |
| Same-user trusted worker can access host | Docker/enforced sandbox default; trusted mode explicit only |
| H20 credential persists in worker home | Ephemeral HOME/GROK_HOME/XDG; env allowlist; canary tests; cleanup |
| Subagent explosion/cost | Request-bound depth/concurrency/turn/time limits in receipt |
| “All delegated” becomes unreviewed authority | Hermes retains adoption/commit/push/deploy approval |
| Sync bot silently enables new feature | Feature harvest is a separate labeled PR after sync |
| Fork becomes product-sized divergence | Quarterly patch-budget review; upstream-first replacement when native equivalent appears |

## 13. Out of scope

- changing default Hermes model/provider;
- sending provider credentials to GitHub Actions;
- forking Human20 Keys;
- replacing Hermes delegation/runtime;
- adding financial/production/publication authority to Grok;
- modifying upstream TUI aesthetics;
- accepting remote PRs on behalf of upstream;
- auto-merging or auto-activating upstream changes;
- hiding that trusted mode is unsandboxed.

## 14. Final acceptance criteria

The redesign is complete only when:

1. `evgyur/grok-build` exists as an Apache-2.0 fork with `upstream` lineage and preserved notices.
2. Fork binary reports exact fork/upstream/source provenance.
3. Official auto-update cannot replace fork binary.
4. Worker contract v1 returns a bounded receipt on success, timeout, sandbox, model and startup failures.
5. Strict sandbox evidence is machine-readable and fail-closed.
6. `review/full` can use bounded subagents.
7. Declared artifacts export automatically; `.venv`/cache residue never gets adopted.
8. `chip-grok` pins binary SHA and rejects wrong/official binaries by default.
9. Continuum review produces and delivers the `.md` plan end-to-end without manual file copying.
10. Upstream update creates one deterministic sync PR with source/changelog/license/CLI diff.
11. New upstream functionality is integrated through explicit adapter PRs, not silently.
12. Release candidate passes mock CI, Docker sandbox E2E, H20 live smoke, fresh install and rollback.
13. Commit/push/release/deploy remain outside worker authority unless separately approved.
