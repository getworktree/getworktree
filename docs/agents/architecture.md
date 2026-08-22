# Architecture

Structural map for agents. **File placement rules** (where models vs services
live) are in
[code-conventions.md — Core package layout](code-conventions.md#core-package-layout).
User-facing command behavior lives under [docs/cli/](../cli/). YAML/config field
detail lives in [schemas-and-config.md](schemas-and-config.md).

## Layers

```
src/worktree/cli/cli.py              Typer entrypoint
src/worktree/cli/<name>/             One package per CLI subcommand (thin wrappers over domain)
  app.py                             Typer app / command registration
  commands/                          Command handlers (e.g. root.py, show.py)

src/worktree/core/                   Business logic (no Typer)
  bootstrap.py                       .worktree/ create/repair
  git_sandbox.py                     git worktree sandbox lifecycle
  config/                            Legacy flat infra (loader/mutate/validate/…)
  db/                                models.py, connection.py, migrations.py, repositories/, alembic/
  inputs/                            models.py + services/ (resolve, interpolate, renderer)
  catalog/                           models.py + services/ + templates/
  blueprint/                         models.py, exceptions.py, renderers.py,
                                     services/{blueprint,run,resume}.py
  history/                           models.py, renderers.py, services.py
  step/                              models.py, exceptions.py, runner.py (entrypoint),
                                     assertions/, services/{loader,resolver}.py
  runtime/                           models.py, exceptions.py, engine.py (entrypoint:
                                     `run_steps`, in-process step-loop orchestration),
                                     failure + pause helpers
  engine/                            models.py, engine.py (entrypoint: `Engine` class,
                                     DB-persisted run/resume process facade)
  task/                              [status: unused by the live CLI — see note below]
                                     models.py, exceptions.py, services/{loader,runner,renderer}.py
  agents/                            models.py, exceptions.py + adapters (base, factory,
                                     providers) — documented layout exception, see
                                     code-conventions.md#core-package-layout
  patch/                             models.py, exceptions.py, patch.py (entrypoint)
  workflows/                         [status: unused by the live CLI — see note below]
                                     models.py, exceptions.py, services/

src/worktree/common/                 Shared helpers (no core/ imports)
src/worktree/schemas/v1/             Versioned JSON Schemas
```

Default for **new** domain code: `models.py` + `services/<verb>.py`. Do not
extend the flat `config/` / `db/` pattern to new domains. See
[code-conventions.md](code-conventions.md#core-package-layout).

Single-step execution: `core/step/` (`runner.py`). Multi-step orchestration:
`core/runtime/` (`engine.py` → `run_steps`). Process facade: `core/engine/`
(`Engine.run` / `Engine.resume`).

> **Naming hazard:** `core/runtime/engine.py` and `core/engine/engine.py` are
> two unrelated modules that share the literal filename `engine.py` in
> sibling-ish packages. They are architecturally distinct (see above), and an
> `import worktree.core.engine` reaching for "the engine" has, in practice,
> been mistaken for `worktree.core.runtime`'s helpers and vice versa. When
> importing either, double-check which package you actually meant before
> adding the import.

> **Live pipeline vs. legacy packages:** the CLI (`wt run`, `wt resume`)
> exclusively goes through `BlueprintRunService`/`BlueprintResumeService` →
> `Engine` → `run_steps`. `core/task/` and `core/workflows/` are older
> catalog-domain packages that predate `core/blueprint/` and are **not**
> imported by `cli/`, `engine/`, or `blueprint/` today — they are exercised
> only by their own tests. Do not add new callers of `task/services/*` or
> `workflows/services/*`, and do not add a third parallel "load a blueprint
> document and run its steps" implementation for a new catalog domain; extend
> `core/blueprint/` instead. If a task/workflow-specific catalog entry point is
> genuinely still needed, it should delegate to `Blueprint.load(...)`/`Engine`
> rather than maintain its own model, pause-store, and resume implementation.
> Consolidating or removing `task/`/`workflows/` is tracked separately; this
> note exists so the packages aren't mistaken for the source of truth in the
> meantime.

### Domain ownership

- **Task** (`core/task/`, currently unused by the live CLI — see note above):
  `TaskDefinition`, catalog loader, `run_task` adapter, plain-text failure
  renderers.
- **Inputs** (`core/inputs/`): `ParameterInput`, CLI resolve, `${{ inputs.* }}`
  interpolation. Must not import step/runtime/task/workflows/agents/patch.
- **Step** (`core/step/`): `StepDefinition`, `StepAssert` / assertions,
  `execute_step`, failure policy types used by blueprints. Must not import
  runtime/task/workflows.
- **Agents** (`core/agents/`): adapter protocol, provider implementations,
  and failure payload models. Must not import step/runtime/task/workflows.
  `config/models.py`'s `AgentProvider` enum is intentionally broader than
  `agents/factory.py`'s implemented providers (schema-valid placeholders for
  providers not built yet — see
  [schemas-and-config.md](schemas-and-config.md#config-v1-contract)); the
  factory is still the single source of truth for *which* providers actually
  run, and any config-valid-but-unimplemented provider must fail with a clear,
  typed error at adapter-resolution time, not a bare `ValueError` surfacing
  mid-run.
- **Patch** (`core/patch/`): unified-diff parse/validate (no git apply).
  Must not import agents/step/runtime/task/workflows.
- **Blueprint** (`core/blueprint/`): unified task/workflow document handle,
  catalog/path load, execution/resume services, and `resolve_inputs` against declared parameters.
- **Runtime** (`core/runtime/`): `run_steps`, `RunContext` / `RunObserver` /
  `RunOutcome`, in-process failure orchestration after a failed step
  (stop / `prompt_user` / retry-or-continue), and durable pause via
  `RunPauseStore` / `RunCheckpoint` hooks. Step-local retry stays in step.
  `RunOutcome.session_id` may be stamped by Engine; `run_steps` does not mint
  it. Runtime must not import task/workflow DB facades or `cli/`.
- **Engine** (`core/engine/`): `RunRequest`, persist run row, mint session id,
  resolve inputs before `run_steps`, stamp `session_id` on `RunOutcome`.
  Must not import `cli/`.
- **Workflows** (`core/workflows/`, currently unused by the live CLI — see
  note above): workflow definition models and `resume_workflow` (rebuilds
  `RunContext` from a paused checkpoint and re-enters `run_steps`). Sibling of
  task — neither imports the other. Domain adapters persist pause checkpoints.
- **Catalog** (`core/catalog/`): blueprint scan/index, `CatalogDb` sync hooks,
  packaged seeds under `templates/`.
- **History** (`core/history/`): `HistoryListService`, `HistoryShowService`,
  result models, and table/panel renderers.
- **Shared core infra**: `config/`, `db/`, `bootstrap.py`, `git_sandbox.py`,
  plus foundational domains above.

CLI: packages are thin wrappers over core domain services and contain no
business logic, database queries, or execution algorithms.

### Package boundaries (import direction)

Dependencies flow one way; do not import "up" the stack:

```
common/  ->  core/{db,catalog,inputs,patch,history}/  ->  core/agents/  ->  core/step/  ->  {core/runtime/, core/blueprint/}  ->  core/engine/  ->  {core/task/, core/workflows/}  ->  cli/
```

- `common/` never depends on `core/`.
- `core/inputs/` must not import `step`, `runtime`, `task`, `workflows`,
  `agents`, or `patch`.
- `core/patch/` must not import `agents`, `step`, `runtime`, `task`, or
  `workflows`.
- `core/agents/` may use `patch/` and `config/`; must not import `step`,
  `runtime`, `task`, or `workflows`.
- `core/step/` must not import `runtime`, `task`, or `workflows` for shared
  vocabulary — put shared types in `common/` or `step/`. Agent dispatch uses
  `core.agents` from the step runner.
- `core/runtime/` may use `step/`, `db/`, `git_sandbox.py`; must not import
  `blueprint/`, `engine/`, `task/`, `workflows/`, or `cli/`.
- `core/blueprint/` may use `catalog/`, `inputs/`, `step/`; must not import
  `runtime/`, `engine/`, `task/`, `workflows/`, or `cli/`.
- `core/engine/` may use `runtime/`, `blueprint/`, `db/`; must not import
  `task/`, `workflows/`, or `cli/`.
- `core/task/` and `core/workflows/` depend on runtime/step/inputs/catalog;
  they do not import each other.
- `cli/` may import `core/` and `common/`; those layers never import `cli/`.

If a lower package needs a type from a higher one, **move the type down**
instead of adding an upward import.

**Watch for prompter/observer construction reaching past `Engine`.**
`blueprint/services/{run,resume}.py` own the CLI-facing run/resume flow, but
constructing a `CliFailurePrompter` or resolving a `RunObserver` requires
`runtime/`, which `blueprint/` is not allowed to import (`blueprint` → `engine`
→ `cli`, not `blueprint` → `runtime` directly). This exact shape produced a
reproducible circular import (`blueprint` → `engine` → `blueprint`) that only
"worked" because import order happened to mask it — a fresh script or test
that imports `worktree.core.engine` before `worktree.core.blueprint` would
have failed immediately. If blueprint-layer code needs a default
prompter/observer, either have `Engine` resolve sane defaults internally (it's
the layer allowed to depend on both `blueprint` and `runtime`), or push
prompter/observer construction up into the CLI command handlers that call
`BlueprintRunService`/`BlueprintResumeService` — never import `runtime`
directly from `blueprint/`.

## Adding a new command

1. Create `src/worktree/cli/<name>/` with lean `app.py` and `commands/<subcommand>.py` (or
   `commands/root.py` for root commands). Use this `commands/` shape for **every**
   new or touched CLI package — `history`, `resume`, and `run` already match it;
   a singular `command.py` is legacy from before this convention landed and is
   not a second valid option. If you touch an existing `command.py` package,
   migrate it to `commands/` in the same change rather than adding to the older shape.
2. Wire command logic directly to underlying domain services (e.g. `BlueprintRunService`,
   `HistoryListService`). The CLI package should not contain actual logic — it is
   simply a wrapper around the domain being executed. Concretely: no
   `SomeRepository(...)` construction, no filesystem scans of catalog/template
   directories, and no reconciliation/detection algorithms in `cli/`. If a CLI
   handler needs a query or an algorithm that doesn't exist in `core/` yet, add it
   as a `core/<domain>/services/` function and call that — don't write it inline
   in the command module just because it's currently only needed by one command.
3. Register in [src/worktree/cli/cli.py](../../src/worktree/cli/cli.py).
4. Tests under `tests/cli/<name>/`.

## Adding a new catalog-backed domain

When creating or refactoring a blueprint domain (e.g. `task`, `workflow`, `step`):

1. **Models**: `<X>Definition` in `core/<x>/models.py`.
2. **Exceptions**: `<X>LoadError` / `<X>ValidationError` subclassing the common
   definition errors in `core/<x>/exceptions.py`.
3. **Loader**: `core/<x>/services/loader.py` → thin
   `get_catalog_item(..., definition_cls=...)`.
4. **Execution**: if it runs steps, build `RunContext` and call
   `run_steps` in `core.runtime.engine` — no duplicate step loops/sandbox
   lifecycle.
5. **CLI**: thin `commands/root.py` (or `commands/<subcommand>.py`); Rich in
   `cli/<x>/renderers.py`; plain-text formatters in
   `core/<x>/services/renderer.py`. No production test-seam parameters
   (`execute_fn=...`).

## Adding a new agent provider

Every provider implements `AgentAdapter.propose_fix(request: AgentRequest) ->
AgentResponse` (`core/agents/base.py`). Before writing a sixth adapter from
scratch, check whether it fits the shared direct-mutation base — most do.

1. Add the provider's token to `AgentProvider` in `core/config/models.py` if it
   isn't already a schema-valid placeholder (`openai`, `anthropic`,
   `azure_openai`, and `custom` are currently reserved-but-unimplemented — see
   [schemas-and-config.md](schemas-and-config.md#config-v1-contract)). One of
   these is likely already the token you want.
2. Pick a shape:
   - **Direct-mutation** (the provider's SDK/CLI edits files in the sandbox
     itself — `cursor`, `gemini`, `copilot` today): subclass
     `CliDirectMutationAdapter` (`core/agents/cli_mutation.py`) and implement
     only `_preflight(request) -> str | None` (env/key/model checks; return an
     error string or `None`), `_provider_name() -> str`, and
     `_default_run(request: CliMutationRunRequest) -> CliMutationOutcome` (the
     actual subprocess/SDK call). The base class already owns baseline capture,
     timeout/error classification, diff capture, and patch-safety validation
     (`max_files`, `max_patch_kb`, binary rejection) — reimplementing any of
     that in the new adapter is the duplication this recipe exists to prevent.
   - **Diff-returning** (the provider only proposes a diff without touching the
     sandbox — `local`, `ollama` today): implement `propose_fix` directly; you
     own the full `no_op` / `unfixable` / `timeout` / `provider_error` /
     `proposed_patch` classification yourself. See `local.py` / `ollama.py`.
3. Resolve the provider's secret the way every existing adapter does: a
   module-level `resolve_<provider>_api_key()` (or equivalent) that reads one
   well-known env var and returns `None` (never raises) when unset — see
   `cursor.py`'s `resolve_cursor_api_key`. The key must never be accepted as a
   `config.json` field or adapter constructor argument. See **Secrets
   handling** below.
4. Register in `get_agent_adapter` (`core/agents/factory.py`): one
   `if provider == "<name>":` branch. If the token from step 1 isn't wired up
   yet, leave it falling through to the existing
   `ValueError(... AGENT_PROVIDER_UNSUPPORTED ...)` rather than adding a
   placeholder branch that returns something for a provider you haven't built.
5. Tests under `tests/core/agents/test_<provider>.py`, injecting a fake
   `run_fn` (direct-mutation) or fake transport (diff-returning). Never hit a
   real network/SDK from a test.

If you catch yourself copy-pasting a preflight check, timeout-handling block, or
prompt-building step from an existing provider for the second time (not the
first — some duplication across exactly two providers is normal while a
pattern is still forming), that's the signal to extract it into
`cli_mutation.py` rather than pasting a third copy.

## Secrets handling

Provider API keys/tokens (`CURSOR_API_KEY`, `GEMINI_API_KEY`, `GH_TOKEN` /
`GITHUB_TOKEN`) are read directly from the process environment by a
provider-specific `resolve_*` function at call time and passed straight into
the SDK/subprocess invocation. They are never:

- accepted as a `config.json` field (`config/models.py`'s `AgentConfig` has no
  key/token/secret field — only `provider`, `model`, `endpoint`, `temperature`,
  `max_tokens`),
- stored on `AgentRequest`, `CliMutationRunRequest`, `RunCheckpoint`, or any
  other Pydantic model that gets `model_dump`'d, persisted to `data.db`
  (history, checkpoints), or written to a sandbox file, or
- included in `build_mutation_prompt`'s prompt body (`core/agents/models.py`'s
  `AgentFailurePayload` carries command output and file contents, not
  environment state).

Keep it that way: if you're adding a field to any model in this chain (`
AgentRequest`, `CliMutationRunRequest`, `RunCheckpoint`, `AgentFailurePayload`),
don't let a secret leak into it just because it's convenient to thread through
- resolve it again at the point of use instead. Command/agent **stdout and
stderr** are captured into `StepResult` and, depending on `history.
save_attempt_logs` / `save_agent_payloads` (see
[schemas-and-config.md](schemas-and-config.md#config-load-api)), persisted to
`data.db` or written under `.worktree/sessions/` — a provider or script that
echoes a secret to stdout will have that secret persisted, and this layer has
no redaction step today. If that's a real risk for a provider you're adding
(e.g. one that logs its auth flow to stdout), say so in that provider's
docstring rather than leaving it to be discovered later.

## The `.worktree/` directory

Created/repaired by
[bootstrap.py](../../src/worktree/core/bootstrap.py) (idempotent; never deletes
user data):

```
.worktree/
  .meta/bootstrap.json
  config.json                 # schemas/v1/config.json
  catalog/                    # workflows/, tasks/, steps/ + seeded wt/ templates
  workflows/                  # legacy bootstrap dir
  sessions/                   # per-session artifacts (e.g. diff.patch)
  artifacts/, tmp/, logs/
  sandboxes/                  # git worktree checkouts
  data.db                     # SQLite (core/db)
```

Catalog dirs/seeds: `core/catalog` (`ensure_catalog_dirs`,
`scan_and_index_catalog`, `seed_all_catalog_templates`).

### Local SQLite (`data.db`)

Migrated by `init_database` in [core/db](../../src/worktree/core/db/__init__.py).
Typed surface: `DbBase`, `BaseRepository`, repos (`SandboxesDb`, `RunsRepository`,
`CatalogDb`, `CostsDb`), facade `WorktreeDb` (`.sandboxes`, `.runs`, …).

Primary tables include sandbox metadata, catalog index rows, run tracking, and
workflow cost rows. Schema evolution stays in `core/db` migrations — do not
document every column here; read models in `core/db/models.py`.

**Construct one repository/facade per command invocation, not one per call.**
`BaseRepository.session()` lazily runs `init_db()` (a full Alembic upgrade
check) the first time a given instance is used.
Constructing `SomeRepository(cwd)` fresh at every call site defeats that
lazy-init entirely — every `.list()`/`.get()`/`.upsert()`/`.delete()` reruns
the migration check, and a loop that constructs a new repository per
iteration turns this into an N+1. Build the repository/facade once per Typer
command invocation (e.g. in the command's entry function or a shared context
object) and pass it down or reuse it across calls in the same command.

Before adding a new table, column, or repository, see the migration hygiene
checklist in
[ci-and-tooling.md](ci-and-tooling.md#migration-hygiene).

## Sandboxes (core)

[GitSandboxManager](../../src/worktree/core/git_sandbox.py) owns create/cleanup
and best-effort `SandboxesDb` writes.

- On-disk: `.worktree/sandboxes/<session_id>/`, branch `worktree/sandbox-<id>`.
- Result API: `create_sandbox_result` → `SandboxCreateResult` (warnings do not
  flip `ok`). `cleanup_sandbox` is idempotent.
- CLI UX (`wt sandbox create|list|show|delete`): [docs/cli/sandbox.md](../cli/sandbox.md).

## Workflows, agents, patches

| Concern | Where |
|---------|--------|
| Workflow YAML | `schemas/v1/workflow.json`, [schemas-and-config.md](schemas-and-config.md) |
| Task YAML | `core/task/models.py`, schemas-and-config |
| Blueprint execution (`wt run`) | [docs/cli/run.md](../cli/run.md), `core/blueprint/` |
| Patch validation | `core/patch/` (`validate_patch_text`) |
| Agent failure payload DTOs | `core/agents/models.py` |
| Agent adapters | `core/agents/` — protocol + `local` / `ollama` / `cursor` / `gemini` / `copilot` |

Provider-specific env vars and stdout contracts belong in code docstrings or
CLI docs when user-visible — not as growing appendices in this file.

## Packaged resources

Schemas and catalog templates ship in the wheel and are loaded via
`importlib.resources` (see `common/schema_validation.py`, workflow validators,
`core/catalog/templates/`), not repo-relative paths at runtime.
