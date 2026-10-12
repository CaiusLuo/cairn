# Working rules for coding agents

Cairn is a Python 3.12 agent harness. These rules are the ones a change is most
likely to violate. Read [docs/architecture.md](docs/architecture.md) before
moving code between packages.

## Environment

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

`pytest` is offline by default. Never run a paid provider or publish to GitHub as
part of a normal change; those flows need separate, explicit authorization.

## Architecture rules

- Dependencies point one way: `terminal → runtime → infra → core`, and `core`
  depends on nothing inside Cairn. `tests/core/test_architecture.py` enforces
  this.
- `core` holds contracts that are meaningful without an adapter. It must not
  import provider SDKs, process, network or persistence modules. Do not add a
  field to a core model because one adapter finds it convenient.
- `infra` performs external effects and owns their safety checks. `runtime`
  sequences work and owns task/turn state. `terminal` presents and parses input.
- Put sequencing in the runtime that owns the operation. Do not add ports,
  registries, factories or event buses without a caller that needs them today.
- Every mutable state, lifecycle and external side effect needs one owner. If two
  modules both advance a lifecycle, that is a defect, not a convenience.

## Behaviour rules

- `runtime.loop.run_turn` is the only Agent turn. Do not add a second loop.
- Tool preflight (existence and argument validity) must precede permission
  checks; authority comes only from granted capabilities, never from model
  arguments.
- Fail closed at trust boundaries: sandbox, path validation, credential
  filtering, publication guards and evidence checks. Do not remove a check
  because it looks duplicated; first establish whether it guards the same trust
  boundary.
- Owning work must finish its cleanup under repeated cancellation. Use
  `cairn.runtime.lifecycle` rather than re-implementing a shield/cancel/settle
  loop.
- Structural changes must not change Agent semantics or evaluation outcomes.

## Tests

Organize tests by ownership, mirroring `src/cairn`. Shared fixtures live in
`tests/support/`; do not import one test module from another. Keep security,
cancellation, recovery, sandbox and freshness coverage intact even when pruning
redundant tests. See [tests/README.md](tests/README.md).

## Documentation

- Documentation describes the current design, not the session that produced it.
  Change history belongs in commits.
- Update links when you move a file; `tests/docs/test_documentation.py` checks
  relative links and referenced paths.
- Never modify frozen evidence: the Repository Intelligence V1 pilot directory,
  its experiment records, dataset digest or historical findings.
