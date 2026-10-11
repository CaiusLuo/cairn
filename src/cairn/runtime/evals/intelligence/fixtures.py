"""Harness-owned Git setup and narrowly allowlisted benchmark memory approval."""

import os
from dataclasses import dataclass
from pathlib import Path

from cairn.core.memory.models import (
    Candidate,
    ClaimScope,
    EvidenceBinding,
    Kind,
    Origin,
    ScopeKind,
)
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.git.environment import build_git_env
from cairn.infra.memory.identity import ProjectIdentity
from cairn.infra.memory.store import MemoryStore
from cairn.infra.tools.bash import _resolve_tmpdir, _resolve_uv_cache_dir
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.checks import FileContentEqualsCheck
from cairn.runtime.evals.models import EvalSuiteCase
from cairn.runtime.memory.evidence import EvidenceResolver, EvidenceSource
from cairn.runtime.memory.service import (
    ApprovalAuthority,
    ApprovalRequest,
    MemoryService,
)


@dataclass(frozen=True, slots=True)
class MemoryFixture:
    name: str
    statement: str
    evidence_files: tuple[str, ...]
    relevant: bool
    complete: bool = True


@dataclass(frozen=True, slots=True)
class IntelligenceCase:
    item: EvalSuiteCase
    memories: tuple[MemoryFixture, ...]
    relevant_files: tuple[str, ...]
    # These changes are materialized before the Agent in EVERY arm.
    current_files: tuple[tuple[str, str], ...] = ()


async def fixture_git(workspace: Workspace, *args: str) -> str:
    env = build_git_env(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Cairn Benchmark",
        GIT_AUTHOR_EMAIL="benchmark@example.invalid",
        GIT_COMMITTER_NAME="Cairn Benchmark",
        GIT_COMMITTER_EMAIL="benchmark@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
    )
    result = await RepoContextProvider(workspace)._run_git(
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args, env=env
    )
    if result.returncode or result.stdout_truncated or result.stderr_truncated:
        raise ValueError("Benchmark Git setup failed")
    return result.stdout.decode().strip()


async def initialize_git(workspace: Workspace) -> None:
    await fixture_git(workspace, "init", "-b", "benchmark")
    await fixture_git(workspace, "add", "--all")
    await fixture_git(workspace, "commit", "-m", "benchmark fixture v1")


def writable_roots(workspace: Workspace) -> tuple[Path, ...]:
    roots = [workspace.root, Path("/tmp").resolve()]
    for root in (_resolve_tmpdir(os.environ), _resolve_uv_cache_dir(os.environ)):
        if root is not None:
            roots.append(root)
    return tuple(roots)


async def approve_fixtures(
    spec: IntelligenceCase,
    workspace: Workspace,
    directory: Path,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[MemoryStore, EvidenceResolver, dict[str, bool]]:
    identity = await ProjectIdentity.inspect(workspace)
    store = MemoryStore(
        directory,
        identity.project_id,
        forbidden_roots=writable_roots(workspace),
        sensitive_values=sensitive_values,
    )
    resolver = EvidenceResolver(workspace, identity.project_id)
    head = await fixture_git(workspace, "rev-parse", "HEAD")
    tree = await fixture_git(workspace, "write-tree")
    relevant: dict[str, bool] = {}
    for fact in spec.memories:
        # Facts are explicit versioned host fixtures; independently check exact
        # evidence source contents before the evaluated model exists.
        for path in fact.evidence_files:
            expected = spec.item.case.files[path]
            checked = await FileContentEqualsCheck(path, expected).evaluate(workspace)
            if not checked.passed:
                raise ValueError("Benchmark memory source verification failed")
        claim = Candidate(
            statement=fact.statement,
            kind=Kind.CONTRACT,
            scope=ClaimScope(
                kind=ScopeKind.EXACT_PATHS,
                paths=tuple(sorted(fact.evidence_files)),
                complete=fact.complete,
            ),
        )

        def approve(request: ApprovalRequest, *, expected: Candidate = claim) -> bool:
            capsule = request.attestation.capsule if request.attestation else None
            return (
                request.operation == "accept"
                and request.entry.candidate == expected
                and capsule is not None
                and capsule.binding.origin is Origin.CODE
                and capsule.binding.candidate_digest == expected.digest()
                and capsule.evidence_oid == tree
                and capsule.source_head_oid == head
            )

        # This allowlist is benchmark-only, based on the user's fixture approval
        # authorization. It is never exposed as a production authority or tool.
        api = MemoryService(store, ApprovalAuthority(approve))
        entry = api.propose(claim)
        entry = await api.accept(
            entry.id,
            entry.version,
            resolver=resolver,
            source=EvidenceSource(head, tree),
            binding=EvidenceBinding(
                candidate_digest=claim.digest(), scope=claim.scope, origin=Origin.CODE
            ),
            actor="benchmark-host",
        )
        relevant[entry.id] = fact.relevant
    return store, resolver, relevant


async def materialize_current(spec: IntelligenceCase, workspace: Workspace) -> None:
    for path, text in spec.current_files:
        workspace.resolve_path(path).write_text(text, encoding="utf-8")
    if spec.current_files:
        await fixture_git(workspace, "add", "--all")
        await fixture_git(workspace, "commit", "-m", "current fixture v1")
