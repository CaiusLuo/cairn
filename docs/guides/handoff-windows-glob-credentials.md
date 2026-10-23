# Hand-off: Windows verification, glob bounds, configurable credentials

**Status**: research only — no code written for these three topics.
**Repo**: cairn, branch `main`, HEAD `f14ec2a`.
**Written**: 2026-10-23.

Read this together with [AGENTS.md](../../AGENTS.md) and
[docs/guides/sandbox.md](sandbox.md) before changing anything.

---

## 0. Baseline: what is already true

Verify these before starting; the hand-off assumes them.

| Fact | Where |
| --- | --- |
| Shell runs locally, unsandboxed, as the user | `src/cairn/infra/tools/bash.py` |
| Capability checks refuse rather than degrade | `src/cairn/infra/platform.py` |
| Workspace trust is asked once at startup | `src/cairn/terminal/cli.py`, `core/permissions.py` |
| Outside reads allowed; outside writes need a per-directory grant | `infra/tools/arguments.py` |
| `glob` can search any directory (`path`), blocks credential names | `infra/tools/glob.py` |
| `ToolResult.error_type` / `blocked_paths` exist | `core/models.py` |

Last verified full suite: **2898 passed, 0 failed** (server, Debian 12).

---

## 1. Windows real-machine verification (from macOS + Linux)

### Options, strongest to weakest

**A. GitHub Actions `windows-latest` matrix — recommended**

Add a `windows-latest` job to `.github/workflows/ci.yml`. GitHub-hosted Windows
runners are free for public repositories, which this one is.

```yaml
jobs:
  quality-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@<same-sha-as-existing>
      - uses: astral-sh/setup-uv@<same-sha>
        with: { python-version: "3.12", enable-cache: true }
      - run: uv sync --locked --all-groups
      - run: uv run mypy src tests
      - run: uv run pytest -q
```

Pros: a real Windows kernel and filesystem, no local maintenance, and it becomes
a required check. Cons: only runs on push/PR; logs instead of an interactive
shell for debugging.

**B. Local QEMU Windows VM (macOS/Linux host)**

The approach documented for Rust CLI work: cross-compile or transfer, boot a
Windows image under QEMU, run tests over SSH. Reference:
[Windows CLI Testing from macOS: Cross-Compilation + QEMU VM](http://fengmk2.com/posts/blog-qwin-windows-testing-on-macos).
Author's measured costs: ~60 min one-time image build; on macOS (TCG, no
hardware acceleration) 2–5 min boot and 3–5× slowdown; 5 min per fix cycle vs
15+ min waiting for CI. On a Linux host with KVM it is near-native.

Pros: interactive debugging, works offline. Cons: a Windows licence, ~10–20 GB
disk, and TCG slowness on Apple Silicon.

**C. Cloud VM (Azure/AWS Windows instance)**

Real Windows, remote, per-hour cost, no local disk. Reasonable if you need an
interactive Windows session often. More setup than A, more cost than B.

**D. Wine / cross-platform simulation — NOT sufficient**

Wine does not reproduce Windows filesystem semantics (case-insensitivity, path
separators, ACLs, `os.name == "nt"`). The existing simulated test in
`tests/infra/test_platform_capabilities.py` deletes POSIX flags before importing
and is valuable for **import-safety regression**, but it must never be recorded
as Windows verification. This distinction is already stated in the repo.

### Recommendation

Start with **A**, because it is the only option that gives a real Windows run
with zero maintenance and gates every future PR. Add **B** only if you need to
debug interactively; the QEMU write-up above is a concrete, costed recipe.

### Concrete gaps to close first

1. `.github/workflows/ci.yml` still runs `sudo apt-get install -y bubblewrap`
   (the "Install Linux sandbox" step). That tool is no longer used by any code
   path; the step should be removed.
2. The workflow is `ubuntu-22.04` only. `run: uv run pytest` will fail on Windows
   wherever a test assumes POSIX (process groups, `fcntl`, `/bin/bash`). The
   existing skips (`tests/support/sandbox.py` and `tests/infra/tools/test_bash.py`) cover some
   of it; a Windows job will reveal the rest, and each reveal is a real finding.
3. `pyproject.toml` declares no OS classifiers. Adding
   `Operating System :: POSIX :: Linux` / `MacOS` documents intent, but platform
   enforcement must come from an explicit check, not metadata.

**Do not** treat a green Windows CI job as proof that Durable sessions work
there — `infra/sessions/paths.py` still refuses non-POSIX with
`unsupported_locking`, by design.

---

## 2. glob recursion depth and scale

### Current behaviour

`src/cairn/infra/tools/glob.py`:

- `MAX_SCAN_CANDIDATES = 50_000` — a cap on accepted matches, **not** on
  directories visited. `root.glob(pattern)` walks until the cap is reached.
- `DEFAULT_LIMIT = 100` returned to the model; `stdout_truncated` marks overflow.
- No gitignore awareness, no symlink following, no depth bound.

The risk: `glob("**/*", path="/Users")` walks the whole tree (including
`node_modules`, `Library/Caches`, `.venv`) until 50k matches land. On a real
home directory that is minutes of I/O, and it can traverse very large trees even
though the model asked for a cheap listing.

### What mature harnesses do

The most complete public design spec surveyed is
[Glob Tool — Cross-Language Design Spec](https://github.com/avifenesh/tools/blob/06f0b0432e181d3249aca12073b66a744c1616de/agent-knowledge/design/glob.md),
which surveys Claude Code, opencode, Gemini CLI, Continue and MCP filesystem.
Its decisions, all directly applicable:

| Decision | Their reasoning |
| --- | --- |
| **Respect `.gitignore` by default** | The model should not glob `node_modules` |
| **No per-call `respect_git_ignore` escape hatch** | "a frustrated model flips them and floods the context"; relax at session config instead |
| **No `max_depth` parameter** | `**/` already encodes depth; a flag creates two sources of truth |
| **Never follow symlinks** | Symlink loops are a denial-of-service vector; matches `rg` default |
| **Sort mtime DESC, path ASC** | Top of list is the intended anchor; tiebreak keeps pagination stable |
| **`head_limit` (default 250) + `offset`** | Paginate without re-scanning |
| **Bounded output** so `**/*` cannot wipe the context window | — |

Also relevant: Claude Code has a filed bug that its Glob **ignores** `.ignore`/
`.rgignore` by default and only skips `.gitignore`
([anthropics/claude-code#97866](https://github.com/anthropics/claude-code/issues/97866)) —
worth reading before choosing which ignore files to honour.

### Options for cairn, smallest change first

**Option 1 — bound the walk, not just the matches (smallest).**
Keep `root.glob`, but count *visited entries* and stop with
`stdout_truncated=True` after a fixed budget. Cheap, no new dependency.
Downside: still walks `node_modules` if it is inside the first N entries.

**Option 2 — skip ignore-file matches (right shape).**
Read `.gitignore` (and decide about `.ignore`) for the search root and prune
matches. Python's `pathspec` library implements gitignore semantics; adding a
dependency needs justification, or implement a minimal matcher for the common
cases (`node_modules/`, `*.log`, `dist/`) and document the limitation.

**Option 3 — delegate to `rg --files` (most faithful, biggest change).**
`rg --files -g <pattern>` gets gitignore handling, `.ignore`, binary skipping and
speed for free, and it is already a tool developers have. Cost: a hard
dependency on ripgrep being installed, plus parsing its output, plus a new
failure mode when it is absent. The surveyed spec chose this route.

### Recommendation

**Option 1 now, Option 3 if and when the model actually searches big trees.**
Option 1 closes the denial-of-service hole without a dependency or a new failure
mode; Option 3 is the better long-term shape but is a separate design decision
and should be an ADR, not a drive-by change.

Whatever you pick: keep `is_credential_path` filtering applied to every candidate
(Never return a credential), and keep the "no matches" message actionable — the
surveyed spec treats a dead-end zero-match result as a design defect.

---

## 3. Configurable credential blacklist

### Current behaviour

`src/cairn/infra/tools/sensitive_paths.py` holds three module constants
(`CREDENTIAL_DIRECTORIES`, `CREDENTIAL_FILENAMES`, `CREDENTIAL_SUFFIXES`) and one
pure predicate `is_credential_path(path)`. It is consulted only by `glob` today.
The list is fixed; there is no user extension.

### What mature agents do

**Claude Code — `permissions.deny` with `Read(...)` rules**

```json
{
  "permissions": {
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "Read(~/.aws/credentials)"
    ]
  }
}
```

Project-wide in `.claude/settings.json`, local-only in
`.claude/settings.local.json`. Documented at
[Configure permissions](https://code.claude.com/docs/en/permissions).

Two lessons from the survey of this pattern
([Protecting Sensitive Files from Agent Context](https://raw.githubusercontent.com/agentpatterns-ai/website/refs/heads/main/security/protecting-sensitive-files.md)):

1. **A deny list alone has silently failed in production** — multiple filed
   reports of explicitly denied files being read without a prompt
   ([#27040](https://github.com/anthropics/claude-code/issues/27040),
   [#6699](https://github.com/anthropics/claude-code/issues/6699)). It must be one
   layer, verified by a test, not the only control.
2. **Gitignore is not a security boundary** — a gitignored file is merely absent
   from *suggestions*; an explicit read still succeeds unless denied. Do not
   conflate "ignored" with "blocked".
3. **Deny must beat allow, and must be enforced before the read**, in the tool
   itself. Advisory instructions ("don't read .env") are the documented failure
   mode.

**Codex CLI — sandbox-level protected paths**

Codex keeps `.git` and `.codex` read-only *even inside writable roots*, at the
sandbox layer rather than in a tool ([Sandboxing Architecture](https://mintlify.wiki/openai/codex/architecture/sandboxing)).
cairn is local-first, so there is no sandbox to lean on; the equivalent has to be
the tool boundary, which is where `is_credential_path` already sits.

**gitleaks** — a mature secret scanner keeps its rules in a committed TOML
config, so patterns are reviewable and extensible without code changes.

### Options for cairn

**Option A — built-in list + project-local extension file.**
Read e.g. `.cairn/sensitive.toml` (already the config directory) with
`deny = ["glob", "read"]` patterns, merged with the built-in defaults.
*Deny only adds; it cannot un-deny a built-in credential.* This is the shape
Claude Code uses and the one a user can review in a diff.

**Option B — extend the existing TOML schema.**
Put credentials in the already-parsed models config. Rejected on sight: that file
is about providers and models, and mixing a security policy into it makes both
harder to reason about.

**Option C — allowlist instead of denylist.**
Unworkable here: glob searches arbitrary trees, so "what may be read" is
everything except credentials. Allowlisting would need to enumerate a whole disk.

### Recommendation

**Option A, deny-only, additive.** Concretely, when you implement it:

- Keep `is_credential_path` as the single owner of the decision; give it an
  optional extra-patterns parameter rather than teaching every caller to merge
  lists (this preserves "one owner per rule").
- A project file may **add** denied patterns and must not remove a built-in one.
  If a user genuinely must read `~/.aws/credentials`, that is an explicit
  operator approval — not a config file silently disabling the check.
- Enforce in the tool, before any read (`glob`, and `read_file`/`grep` when they
  grow the same outside-read capability). A reviewer must be able to see that
  the check happens before the effect.
- Write the failing test first: a denied path must produce zero bytes in the
  result and a structured `permission_required` outcome. Given lesson (1) above,
  an untested deny rule is the thing that will be silently wrong.
- Document the precedence in `docs/guides/sandbox.md` next to the existing table;
  do not add a second, competing description elsewhere.

---

## 4. Suggested slice order for the next session

1. Remove the dead `bubblewrap` install step from CI (5 min, unblocks Windows).
2. Add the `windows-latest` job; let it reveal real POSIX assumptions one at a
   time. Record each as a finding rather than a skip where possible.
3. glob Option 1 (bounded walk), with a test that a huge tree stops early.
4. Configurable credentials (Option A), test-first, deny-only.
5. Revisit glob Option 3 only with an ADR.

Do steps 1–2 first: a Windows job is the cheap way to find out which of the
remaining items even matters.

---

## 5. Open questions for the human

1. Is a paid Windows CI runner acceptable if the free tier is insufficient, or
   should everything stay on free runners?
2. Windows interactive debugging: is a local QEMU image worth ~20 GB and an hour
   of setup, or is CI-only enough?
3. For glob, is "stop early and report truncation" acceptable, or do you expect
   a complete listing of a large tree (which implies `rg`, and a dependency
   decision)?
4. Credential policy: should a project file ever be able to *un-deny* a built-in
   credential path (weakening the default), or must weakening always require an
   explicit per-session operator approval?

---

## 6. Sources

- [Windows CLI Testing from macOS: Cross-Compilation + QEMU VM](http://fengmk2.com/posts/blog-qwin-windows-testing-on-macos) — measured QEMU costs
- [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) — runner images and limits
- [Glob Tool — Cross-Language Design Spec](https://github.com/avifenesh/tools/blob/06f0b0432e181d3249aca12073b66a744c1616de/agent-knowledge/design/glob.md) — survey of Claude Code / opencode / Gemini CLI / Continue
- [anthropics/claude-code#97866](https://github.com/anthropics/claude-code/issues/97866) — Glob ignores `.ignore`/`.rgignore`
- [Configure permissions (Claude Code)](https://code.claude.com/docs/en/permissions) — `permissions.deny` shape
- [Protecting Sensitive Files from Agent Context](https://raw.githubusercontent.com/agentpatterns-ai/website/refs/heads/main/security/protecting-sensitive-files.md) — deny-list reliability caveats, gitignore-is-not-a-boundary
- [anthropics/claude-code#27040](https://github.com/anthropics/claude-code/issues/27040), [#6699](https://github.com/anthropics/claude-code/issues/6699) — filed deny-rule failures
- [Codex CLI Sandboxing Architecture](https://mintlify.wiki/openai/codex/architecture/sandboxing) — protected paths at the sandbox layer
