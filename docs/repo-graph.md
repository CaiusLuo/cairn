# Repository graph

RepoGraph is a bounded, read-only snapshot of a Workspace's files and code
structure. It uses local filesystem inspection and Python's standard library;
it does not execute project code, call models, or contact network services.
It is independent of conversation state and is not Project Memory.

```python
from pathlib import Path
from cairn.repo_graph import RepoGraphBuilder, BuildLimits, Language
from cairn.workspace.workspace import Workspace

builder = RepoGraphBuilder(
    Workspace(Path(".")),
    limits=BuildLimits(),
    python_roots=("src",),  # Default: (".",); explicitly choose import roots.
)
candidate = builder.build()
if candidate.report.complete:
    files = candidate.list_files(language=Language.PYTHON, limit=20)
    symbols = candidate.find_symbols(name="run_turn", limit=10)
    summary = candidate.summary_for_prompt(max_bytes=2048)
```

## Facts and queries

Snapshots contain immutable file, directory, language, manifest, Python module,
symbol, import and dependency records. Paths are Workspace-relative; code facts
include source lines and the inspected file's SHA-256. Ordering is deterministic.

Python AST extraction covers top-level classes, functions and async functions,
and syntactic imports at any depth. Module names are relative to the longest
matching configured Python root. Relationships resolve only unique parsed
module identities; ambiguous, unsupported and unresolved relationships remain
explicit. Standard-library imports may be classified external. This does not
prove imports execute or succeed at runtime, and imported members are not
assumed to be submodules. Other languages receive inventory only.

`list_files`, `find_symbols`, `module_for_path` and `dependencies_of` return typed
results with `complete`, `truncated` and bounded `unsupported_paths`. Filters
use exact names, enum values and normalized relative paths; file path prefixes
match directory boundaries. Queries default to 20 records, allow at most 200,
and cap compact JSON metadata at 64 KiB. Prompt summaries default to 4 KiB and
allow at most 8 KiB. They contain selected metadata, never source contents or a
full graph dump. Nothing is automatically registered with an Agent.

Manifest metadata is limited to safe project and dependency names:

| Manifest | Extracted scope |
| --- | --- |
| `pyproject.toml` | `[project]` name, direct and optional requirements |
| `package.json` | Name, dependency/dev/peer/optional dependency names |
| `pom.xml` | Direct Maven coordinates and dependencies; no property expansion |
| `go.mod` | Module and require declarations; limited go/toolchain syntax |
| `Cargo.toml` | Package name, direct/dev/build dependency names |
| Gradle files | Presence only; no evaluation |

Malformed, unsupported and limited metadata have distinct statuses. This is
shallow extraction, not a build-system validator: workspace aggregation,
conditional dependency evaluation and arbitrary manifest grammar are outside
the supported scope.

## Bounds and refresh

Default limits are 10,000 entries, depth 32, 512 KiB per file, 16 MiB total reads,
128 manifest dependencies, 100 diagnostics, 50,000 AST nodes per file and 10,000
combined symbol/import records. Name sizes are also bounded. Directory overflow
discards that directory's candidate listing rather than retaining an arbitrary
filesystem-order subset; detecting overflow may inspect one extra entry.

Mandatory exclusions include `.git`, `.cairn`, `.env`, dependency/cache folders,
virtual environments and common generated/build directories; see
`DEFAULT_EXCLUSIONS` in `repo_graph/builder.py`. Caller exclusions add basenames.
Git ignore rules are not interpreted. Symlinks, including internal links, are
never followed; symlink metadata, special files and binary files are explicit
policy skips. Oversized/unreadable files and resource limits make a build
incomplete. Unsupported source structure also makes relevant queries incomplete.

`refresh()` and `rebuild()` both perform a full bounded inspection. They return
the new candidate; check its report before use. Only complete candidates replace
`builder.snapshot`, the last complete snapshot. `builder.last_report` describes
the latest completed attempt. An incomplete refresh keeps the old snapshot;
unexpected exceptions propagate without replacing it. Existing snapshots never
change in place, and separate builders/Workspaces share no index.

Inspection uses descriptor-relative, no-follow reads and checks file/directory
identities before and after inspection. Detected drift makes the result
incomplete. This is not an atomic filesystem transaction or a live freshness
guarantee: changes after the final check require another explicit refresh.
The implementation targets local POSIX filesystems on Linux and macOS; there is
no watcher, persistent cache, Git index mutation or default Agent integration.
