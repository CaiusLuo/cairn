"""Explicit, manual Issue workflow for a disposable GitHub repository."""

import argparse
import asyncio
import os
import re
from pathlib import Path

from dotenv import dotenv_values

from cairn.config import (
    ConfigLayout,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.core.budget import RunBudget
from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.evals.checks import FileContentEqualsCheck
from cairn.git import WorktreeProvider
from cairn.github import (
    GitHubRepository,
    GitHubREST,
    GitHubRESTIssueReader,
    GitHubTaskSource,
    IssueReference,
)
from cairn.github.publisher import GitHubPublisher
from cairn.github.transport import GitHubRESTPublisher
from cairn.llm.provider_runtime import build_provider_runtime
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.tasks import CodingTaskRunner
from cairn.terminal.output import confirm_provider_access, console_permission_prompt
from cairn.workflow import FixedChecksVerifier, LocalWorkflow, WorkflowStatus
from cairn.workspace.workspace import Workspace


async def main(args: argparse.Namespace) -> int:
    if not args.disposable_repository:
        raise ValueError(
            "Opt in with --disposable-repository before any network access"
        )
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.token_env) is None:
        raise ValueError("Invalid credential environment variable name")
    repository = GitHubRepository(owner=args.owner, name=args.repo)
    reference = IssueReference(repository=repository, number=args.issue)
    source = Workspace(args.source)
    parent = args.worktree_parent.resolve()
    if parent.is_relative_to(source.root):
        raise ValueError("Worktree parent must be outside the source directory")
    permissions = SessionPermissionHandler(console_permission_prompt)
    permission = permissions.authorize(
        PermissionRequest(
            capability=PermissionCapability.NETWORK,
            justification=f"Read {repository.full_name} issue #{args.issue}.",
            tool_call=ToolCall(
                id="cairn-read-issue", name="github_issue_read", arguments={}
            ),
        )
    )
    if (
        not permission.allowed
        or PermissionCapability.NETWORK not in permission.granted_capabilities
    ):
        print("Issue read denied.")
        return 1
    client = GitHubREST(token_provider=lambda: os.environ.get(args.token_env))
    issue = await GitHubTaskSource(GitHubRESTIssueReader(client)).fetch(
        reference,
        target_repository=repository,
        base_branch=args.base_branch,
        intended_fix=args.intended_fix,
    )
    values = dotenv_values(source.root / ".env")
    project = load_project_providers(source.root / ".cairn/models.toml")
    provider = default_provider(project, os.environ, values)
    group = provider.config.model_config[0]
    if (
        project.layout is not ConfigLayout.ENV
        and confirm_provider_access(provider, group, switching=False) is not True
    ):
        raise ValueError("Model provider access denied")
    runtime = build_provider_runtime(
        provider,
        resolve_provider_api_key(provider.config, os.environ, values),
        resolve_context_budget(os.environ, values),
    )
    expected = args.expected_file.read_text(encoding="utf-8")
    secrets = frozenset({args.token_env}) | frozenset(
        entry.config.api_key_env for entry in project.providers or (provider,)
    )
    print(f"Retained worktrees and recovery reports: {parent}", flush=True)

    def runner(workspace: Workspace) -> CodingTaskRunner:
        return CodingTaskRunner(
            workspace=workspace,
            llm=runtime.llm,
            context_builder=runtime.context_builder,
            model_executor=runtime.model_executor,
            budget=RunBudget(max_steps=20),
            permission_handler=permissions,
            tracer=Tracer(JsonlTraceSink(parent / "traces")),
        )

    result = await LocalWorkflow(
        WorktreeProvider(source, parent),
        runner,
        FixedChecksVerifier([FileContentEqualsCheck(args.check_file, expected)]),
        secret_env_keys=secrets,
    ).run(issue, base_ref=args.base_ref)
    print(f"Local result: {result.report.status.value}")
    print(f"Worktree: {result.handle.path}\nRecovery: {result.recovery_path}")
    if result.report.status is not WorkflowStatus.VERIFIED:
        print(f"Blocked: {result.report.failure}")
        return 1
    if not args.publish:
        print("Verified locally. Publication requires --publish and NETWORK approval.")
        return 0
    published = await GitHubPublisher(
        GitHubRESTPublisher(client),
        permission_handler=permissions,
        author_name=args.author_name,
        author_email=args.author_email,
    ).publish(issue, result)
    print(f"Publication: {published.report.status.value}")
    if published.report.pr_url is not None:
        print(published.report.pr_url)
    else:
        print(
            f"Blocked: {published.report.failure}; recovery: {published.recovery_path}"
        )
    return 0 if published.report.status is WorkflowStatus.PUBLISHED else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--disposable-repository", action="store_true")
    result.add_argument("--owner", required=True)
    result.add_argument("--repo", required=True)
    result.add_argument("--issue", required=True, type=int)
    result.add_argument("--source", required=True, type=Path)
    result.add_argument("--worktree-parent", required=True, type=Path)
    result.add_argument("--base-ref", required=True)
    result.add_argument("--base-branch", required=True)
    result.add_argument("--check-file", required=True)
    result.add_argument("--expected-file", required=True, type=Path)
    result.add_argument("--token-env", default="GITHUB_TOKEN")
    result.add_argument("--publish", action="store_true")
    result.add_argument("--intended-fix", action="store_true")
    result.add_argument("--author-name", default="Cairn")
    result.add_argument("--author-email", default="cairn@localhost")
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except (OSError, ValueError, RuntimeError) as exc:
        # No arbitrary provider/transport exception content is printed.
        print(f"Workflow stopped: {type(exc).__name__}")
        for note in getattr(exc, "__notes__", ()):
            print(note)
        raise SystemExit(1) from None
